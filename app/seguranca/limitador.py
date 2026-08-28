"""Rate limiting em memória (janela deslizante), sem dependência nova.

Escopo deliberadamente pequeno: não é anti-DDoS (isso é trabalho de proxy/firewall na
borda), é freio contra força bruta no login e abuso/varredura no endpoint reativo
(`/api/leitura` é público por design — ver app/servidor.py). Mesmo padrão de
`app/seguranca/sessao.py`: dict protegido por lock, sem dependência externa.
"""
from __future__ import annotations
import threading
import time

_lock = threading.Lock()
# (bucket, chave) -> timestamps das chamadas aceitas dentro da janela corrente
_contadores: dict[tuple[str, str], list[float]] = {}


def permitido(bucket: str, chave: str, limite: int, janela_seg: float) -> bool:
    """True e já registra a chamada se ainda houver espaço na janela; False (não
    registra) se o limite já foi atingido — quem chamou deve recusar a requisição."""
    agora = time.time()
    corte = agora - janela_seg
    k = (bucket, chave)
    with _lock:
        ts = _contadores.setdefault(k, [])
        while ts and ts[0] < corte:
            ts.pop(0)
        if len(ts) >= limite:
            return False
        ts.append(agora)
        return True


def limpar_antigos(inatividade_seg: float = 3600) -> int:
    """Remove buckets sem chamada recente — sem isso o dict cresce sem limite conforme
    IPs/CNPJs diferentes vão aparecendo ao longo da vida do processo. Chamar
    periodicamente (ligado à mesma limpeza de sessões expiradas, ver sessao.py)."""
    agora = time.time()
    corte = agora - inatividade_seg
    with _lock:
        vazios = [k for k, ts in _contadores.items() if not ts or ts[-1] < corte]
        for k in vazios:
            del _contadores[k]
        return len(vazios)


def _resetar_para_teste() -> None:
    """Zera todos os contadores — a suíte de testes chama isto entre casos (ver
    testes/unitarios/conftest.py), senão chamadas de /login de um teste anterior
    contam contra o limite de outro (todas batem no mesmo IP "testclient" do
    TestClient, já que o dict é global ao processo, não por request)."""
    with _lock:
        _contadores.clear()
