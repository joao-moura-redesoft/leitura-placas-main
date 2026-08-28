"""Freio de força bruta no login: atraso progressivo e bloqueio temporário.

O bcrypt custo 12 encarece cada tentativa (~0,2s), mas sozinho não impede nada: um
atacante paralelizando ainda faz milhares de tentativas por hora contra uma senha
fraca, e não deixava rastro nenhum no log. Aqui as falhas são contadas por e-mail E
por IP — só por e-mail, dá para varrer muitos e-mails a partir de um IP; só por IP,
uma botnet contorna atacando o mesmo e-mail de vários lugares.

Em memória de propósito: é um freio, não um registro. Um restart limpar os contadores
é aceitável (o atacante não controla quando reiniciamos), e assim não há escrita no
banco no caminho de um endpoint público — que é justamente o que um ataque de força
bruta tentaria saturar.
"""
from __future__ import annotations
import threading
import time

# Tentativas falhas toleradas antes de começar a bloquear.
_LIMITE = 5
# Janela em que as falhas são contadas: ficar `_JANELA` sem errar zera o contador.
_JANELA = 15 * 60
# Bloqueio aplicado ao estourar o limite, dobrando a cada falha seguinte até o teto.
_BLOQUEIO_BASE = 30
_BLOQUEIO_MAX = 15 * 60

_lock = threading.Lock()
# chave ("email:x" | "ip:y") → (falhas, momento_da_ultima_falha)
_falhas: dict[str, tuple[int, float]] = {}


def _chaves(email: str, ip: str, escopo: str = "login") -> list[str]:
    """As duas chaves de um balde: por e-mail e por IP, AMBAS dentro do escopo.

    O escopo no prefixo do IP é a correção do achado A6 (auditoria 27/08/2026). Antes a
    chave de IP era `ip:{ip}` seca, compartilhada por todos os fluxos — então
    `/esqueci-senha`, que chama `registrar_falha` INCONDICIONALMENTE (inclusive quando o
    e-mail é enviado com sucesso), enchia o balde de IP que o LOGIN consulta.

    Atrás de proxy ou NAT — o caso do Docker — `request.client.host` é o mesmo para todo
    mundo: 10 POSTs em /esqueci-senha trancavam o login do sistema inteiro por 15 minutos,
    renováveis. `auth.py` já tentava separar os fluxos passando `reset:{email}`, mas a
    separação só valia na dimensão e-mail.
    """
    return [f"{escopo}:email:{email.strip().lower()}", f"{escopo}:ip:{ip}"]


def _bloqueio_restante(chave: str, agora: float) -> float:
    n, ultima = _falhas.get(chave, (0, 0.0))
    if n < _LIMITE or agora - ultima > _JANELA:
        return 0.0
    espera = min(_BLOQUEIO_BASE * (2 ** (n - _LIMITE)), _BLOQUEIO_MAX)
    return max(0.0, ultima + espera - agora)


def segundos_de_bloqueio(email: str, ip: str, escopo: str = "login") -> int:
    """Quanto falta para esta combinação poder tentar de novo (0 = liberado)."""
    agora = time.time()
    with _lock:
        return int(max(_bloqueio_restante(k, agora)
                       for k in _chaves(email, ip, escopo)) + 0.999)


def registrar_falha(email: str, ip: str, escopo: str = "login") -> None:
    agora = time.time()
    with _lock:
        for k in _chaves(email, ip, escopo):
            n, ultima = _falhas.get(k, (0, 0.0))
            # Passou a janela inteira sem errar → recomeça a contagem em vez de somar
            # a uma sequência antiga que já não diz nada sobre agora.
            n = n + 1 if agora - ultima <= _JANELA else 1
            _falhas[k] = (n, agora)


def registrar_sucesso(email: str, ip: str) -> None:
    with _lock:
        for k in _chaves(email, ip):
            _falhas.pop(k, None)


def limpar_expiradas() -> int:
    """Descarta contadores que já saíram da janela — sem isso o dicionário cresce com
    um par de entradas por e-mail/IP que já tentou, para sempre."""
    agora = time.time()
    with _lock:
        velhas = [k for k, (_, ultima) in _falhas.items() if agora - ultima > _JANELA]
        for k in velhas:
            del _falhas[k]
    return len(velhas)


def _resetar_para_teste() -> None:
    with _lock:
        _falhas.clear()
