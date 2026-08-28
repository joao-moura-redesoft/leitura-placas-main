"""Helper de encerramento de thread — reusado pelos `parar()` de WorkerSupervisor,
RetentionWorker, _Encoder (HLS) e pelo `parar_coletor` (captura de dataset).

Os quatro reimplementavam a mesma sequência (seta flag externamente, `join(timeout)`,
loga e devolve False se ainda viva) com timeouts e mensagens de log diferentes. A lógica
de espera é a mesma nos quatro; só o QUE fazer no timeout muda — por isso o callback
`ao_expirar`, em vez de um parâmetro de mensagem fixo. (Review de 28/08/2026, achado B2.)

NÃO seta nenhuma flag de parada — quem chama já deve ter sinalizado (`Event.set()`) antes
de invocar isto. Aqui só cuida do `join` com timeout e do log/retorno quando ela não
confirma a tempo.
"""
from __future__ import annotations
import threading
from typing import Callable


def encerrar_thread(thread: threading.Thread | None, timeout: float,
                    ao_expirar: Callable[[], None]) -> bool:
    """Espera `thread` terminar até `timeout` segundos.

    True se `thread` já é None/estava morta, ou se terminou dentro do prazo. False (depois
    de chamar `ao_expirar()`) se `timeout` estourou e ela continua viva.
    """
    if thread is None or not thread.is_alive():
        return True
    thread.join(timeout=timeout)
    if thread.is_alive():
        ao_expirar()
        return False
    return True
