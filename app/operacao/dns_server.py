"""
Servidor DNS local embutido — puro stdlib, zero dependências extras.

Responde queries para o hostname configurado (ex: lpr.redesoft) com o IP
da interface de rede local deste servidor e encaminha todo o restante para
o DNS upstream (padrão: 8.8.8.8).

Ativação em config.txt:
    dns_ativo = sim
    dns_nome  = lpr.redesoft
    dns_upstream = 8.8.8.8

Permissão no Linux (porta 53 exige privilégio):
    sudo setcap 'cap_net_bind_service=+ep' $(which python3)
    # ou, em virtualenv:
    sudo setcap 'cap_net_bind_service=+ep' $(poetry env info --executable)
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading

log = logging.getLogger("alpr.dns")


# ── Utilitários de rede ───────────────────────────────────────────────────────

def _ip_local() -> str:
    """Detecta o IP da interface LAN (não loopback) sem abrir conexão real."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _origem_confiavel(ip: str) -> bool:
    """True se `ip` pertence a uma faixa privada/local (RFC 1918, loopback, link-local).

    Este servidor faz bind em 0.0.0.0:53 e encaminha queries desconhecidas para o
    upstream — sem essa checagem, um pacote UDP de origem forjada vindo da internet
    (porta 53 exposta por erro de firewall) transformaria o processo num open resolver,
    o desenho clássico usado em ataques de amplificação/reflexão DNS contra terceiros.
    `is_private` do stdlib já cobre 10/8, 172.16/12, 192.168/16, 127/8 e 169.254/16.
    """
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


# ── Parsing de pacotes DNS ────────────────────────────────────────────────────

def _parse_qname(data: bytes, offset: int) -> tuple[str, int]:
    """Extrai o QNAME de um pacote DNS. Suporta ponteiros de compressão."""
    labels: list[str] = []
    visitados: set[int] = set()

    while offset < len(data):
        if offset in visitados:
            break
        visitados.add(offset)

        length = data[offset]

        if length == 0:
            offset += 1
            break

        if (length & 0xC0) == 0xC0:
            # Ponteiro de compressão (RFC 1035 §4.1.4)
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            nome_ptr, _ = _parse_qname(data, ptr)
            labels.append(nome_ptr)
            offset += 2
            break

        offset += 1
        labels.append(data[offset: offset + length].decode("ascii", errors="replace"))
        offset += length

    return ".".join(labels), offset


def _build_resposta_a(query: bytes, ip: str) -> bytes:
    """
    Constrói uma resposta DNS com um registro A apontando para `ip`.
    Formata o header corretamente e copia a seção Question do query original.
    """
    tx_id   = query[:2]
    # Flags: QR=1 (resposta), AA=1 (autoritativo), RD=1 (recursão desejada), RA=1
    flags   = struct.pack(">H", 0x8180)
    qdcount = struct.pack(">H", 1)   # 1 pergunta
    ancount = struct.pack(">H", 1)   # 1 resposta
    other   = struct.pack(">HH", 0, 0)   # NSCOUNT, ARCOUNT

    # Copia a seção Question do query original (offset 12 até o fim do QNAME + QTYPE + QCLASS)
    pos = 12
    while pos < len(query):
        n = query[pos]
        if n == 0:
            pos += 1
            break
        if (n & 0xC0) == 0xC0:
            pos += 2
            break
        pos += n + 1
    question = query[12: pos + 4]   # + QTYPE (2) + QCLASS (2)

    # Registro A: ponteiro → QNAME, tipo A, classe IN, TTL 60 s, RDATA = 4 bytes
    answer = (
        b"\xc0\x0c"                          # ponteiro para QNAME (posição 12)
        + struct.pack(">HHI", 1, 1, 60)      # type A, class IN, TTL 60 s
        + struct.pack(">H", 4)               # RDLENGTH = 4
        + socket.inet_aton(ip)               # endereço IPv4
    )

    return tx_id + flags + qdcount + ancount + other + question + answer


def _build_nxdomain(query: bytes) -> bytes:
    """Resposta NXDOMAIN para queries inválidas ou não encaminháveis."""
    tx_id = query[:2]
    flags = struct.pack(">H", 0x8183)   # QR=1, RCODE=3 (NXDOMAIN)
    zeros = struct.pack(">HHHH", 1, 0, 0, 0)
    pos = 12
    while pos < len(query):
        n = query[pos]
        if n == 0:
            pos += 1
            break
        if (n & 0xC0) == 0xC0:
            pos += 2
            break
        pos += n + 1
    question = query[12: pos + 4]
    return tx_id + flags + zeros + question


# ── Encaminhamento upstream ───────────────────────────────────────────────────

def _encaminhar(data: bytes, upstream: str) -> bytes | None:
    """Repassa a query para o servidor DNS upstream e retorna a resposta."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(data, (upstream, 53))
        resp, _ = s.recvfrom(512)
        return resp
    except Exception:
        return None
    finally:
        s.close()


# ── Servidor ──────────────────────────────────────────────────────────────────

class DNSServer:
    """
    Servidor DNS UDP leve que resolve o hostname do ALPR para o IP local
    e encaminha todo o restante para o DNS upstream.

    Sem rate-limit por IP (decisão deliberada, não esquecimento): o filtro de origem
    privada em `_origem_confiavel` já reduz a superfície de abuso a hosts dentro da
    própria rede local/VPN — não a internet aberta, onde rate-limit por IP importaria
    para conter um cliente malicioso específico. Dentro da rede confiável, um vizinho
    malicioso já teria vetores mais diretos que abusar deste resolver; adicionar
    rate-limit aqui teria custo de código/estado sem reduzir risco real. Se este
    servidor um dia passar a aceitar origem pública, revisar esta decisão.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._parar = threading.Event()
        self._hostname = "lpr.redesoft"
        self._upstream = "8.8.8.8"
        self._ip: str = "127.0.0.1"

    @property
    def ip_local(self) -> str:
        return self._ip

    @property
    def hostname(self) -> str:
        return self._hostname

    def iniciar(self, hostname: str, upstream: str = "8.8.8.8") -> bool:
        """
        Inicia o servidor DNS em thread daemon.
        Retorna True se a thread foi criada (não garante que o bind teve sucesso —
        erros de permissão são logados dentro da thread).
        """
        self._hostname = hostname.lower().rstrip(".")
        self._upstream = upstream
        self._ip = _ip_local()
        self._parar.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="alpr-dns"
        )
        self._thread.start()
        return True

    def parar(self) -> None:
        self._parar.set()

    def _loop(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", 53))
            sock.settimeout(1.0)
        except PermissionError:
            log.error(
                "DNS: sem permissão para bind na porta 53.\n"
                "  Execute UMA vez no servidor:\n"
                "    sudo setcap 'cap_net_bind_service=+ep' $(which python3)\n"
                "  (para virtualenv, substitua 'python3' pelo caminho do venv)"
            )
            return
        except OSError as exc:
            log.error("DNS: falha ao iniciar. %s", exc)
            return

        log.info("DNS local ativo: %s → %s", self._hostname, self._ip)

        while not self._parar.is_set():
            try:
                data, addr = sock.recvfrom(512)
            except socket.timeout:
                continue
            except Exception:
                break

            if not _origem_confiavel(addr[0]):
                # Descarta em silêncio — nem NXDOMAIN, nem log fora do nível debug.
                # Responder qualquer coisa a uma origem não confiável (mesmo um erro)
                # dá a um atacante externo um oráculo de "a porta 53 está aberta".
                log.debug("DNS: pacote de origem não privada ignorado: %s", addr[0])
                continue

            try:
                qname, _ = _parse_qname(data, 12)
                qname_lower = qname.lower().rstrip(".")

                if qname_lower == self._hostname:
                    resp = _build_resposta_a(data, self._ip)
                    sock.sendto(resp, addr)
                    log.debug("DNS: %s → %s (cliente: %s)", qname, self._ip, addr[0])
                else:
                    resp = _encaminhar(data, self._upstream)
                    # Confere que a resposta do upstream tem o mesmo transaction ID
                    # (primeiros 2 bytes do header, RFC 1035 §4.1.1) do query enviado —
                    # sem isso, uma resposta forjada/injetada endereçada ao socket
                    # efêmero usado em `_encaminhar` seria repassada ao cliente como se
                    # fosse legítima (cache poisoning).
                    if resp and len(resp) >= 2 and resp[:2] == data[:2]:
                        sock.sendto(resp, addr)
                    elif resp:
                        log.warning(
                            "DNS: resposta do upstream com transaction ID divergente, descartada")
                        sock.sendto(_build_nxdomain(data), addr)
                    else:
                        sock.sendto(_build_nxdomain(data), addr)

            except Exception as exc:
                log.debug("DNS: erro ao processar pacote de %s: %s", addr, exc)

        sock.close()
        log.info("DNS: encerrado")


# Singleton usado pelo servidor.py
dns_server = DNSServer()
