"""Envio de e-mail via SMTP (biblioteca padrão, sem dependência nova) — usado só por
"esqueci minha senha" e convite de usuário novo.

Fica INERTE (loga e devolve False, não levanta exceção) se `smtp_host` não estiver
configurado — os dois recursos acima detectam isso e caem num aviso "peça a um
administrador" em vez de quebrar. E-mail é o único jeito de entregar um link de
redefinição de senha com segurança (a alternativa seria mostrar a senha nova na tela,
que documentação nenhuma de segurança recomenda), mas nada no resto do sistema
depende disto pra funcionar.
"""
from __future__ import annotations
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core import config

log = logging.getLogger(__name__)


def configurado(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else config.carregar()
    return bool(cfg.get("smtp_host", "").strip())


def enviar(destinatario: str, assunto: str, corpo_texto: str, cfg: dict | None = None) -> bool:
    """True se o e-mail foi entregue ao servidor SMTP. False (sem levantar exceção) se
    SMTP não está configurado ou o envio falhou — quem chama decide como reagir."""
    cfg = cfg if cfg is not None else config.carregar()
    host = cfg.get("smtp_host", "").strip()
    if not host:
        log.warning("E-mail '%s' para %s NÃO enviado: smtp_host não configurado (/configuracao)",
                    assunto, destinatario)
        return False

    try:
        porta = int(cfg.get("smtp_porta", "587") or 587)
    except ValueError:
        porta = 587
    usuario = cfg.get("smtp_usuario", "").strip()
    senha = cfg.get("smtp_senha", "")
    remetente = cfg.get("smtp_remetente", "").strip() or usuario or "alpr@localhost"
    usar_tls = cfg.get("smtp_tls", "sim").strip().lower() in ("sim", "true", "1", "yes")

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destinatario
    msg.set_content(corpo_texto)

    try:
        with smtplib.SMTP(host, porta, timeout=10) as smtp:
            if usar_tls:
                smtp.starttls(context=ssl.create_default_context())
            if usuario:
                smtp.login(usuario, senha)
            smtp.send_message(msg)
        log.info("E-mail '%s' enviado para %s", assunto, destinatario)
        return True
    except Exception as e:
        log.error("Falha ao enviar e-mail '%s' para %s: %s", assunto, destinatario, e)
        return False


def url_base(request, cfg: dict | None = None) -> str:
    """Base pra montar links absolutos nos e-mails — `url_base` configurado tem
    prioridade (obrigatório atrás de proxy reverso, onde o host visto pelo servidor
    pode não ser o público); sem isso, cai no host da própria requisição."""
    cfg = cfg if cfg is not None else config.carregar()
    configurada = cfg.get("url_base", "").strip().rstrip("/")
    if configurada:
        return configurada
    return str(request.base_url).rstrip("/")
