"""Páginas HTML — Jinja2."""
from __future__ import annotations
import config
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
def index(request: Request):
    cfg = config.carregar()
    if cfg.get("implantado", "nao").lower() != "sim":
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse(request, "index.html")


@router.get("/setup")
def setup(request: Request):
    return templates.TemplateResponse(request, "setup.html")


@router.get("/historico")
def historico(request: Request):
    return templates.TemplateResponse(request, "historico.html")


@router.get("/listas")
def listas(request: Request):
    return templates.TemplateResponse(request, "listas.html")


@router.get("/dashboard")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@router.get("/cameras")
def cameras(request: Request):
    return templates.TemplateResponse(request, "cameras.html")


@router.get("/roi/{camera_id}")
def roi(request: Request, camera_id: int):
    return templates.TemplateResponse(request, "roi.html", {"camera_id": camera_id})


@router.get("/configuracao")
def configuracao(request: Request):
    return templates.TemplateResponse(request, "configuracao.html")


@router.get("/testes")
def testes(request: Request):
    return templates.TemplateResponse(request, "testes.html")


@router.get("/documentacao")
def documentacao(request: Request):
    return templates.TemplateResponse(request, "documentacao.html")
