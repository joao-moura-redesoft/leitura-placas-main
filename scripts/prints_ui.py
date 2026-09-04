# -*- coding: utf-8 -*-
"""Tira prints das telas do ALPR para conferencia visual da auditoria de UI/UX.

Usa o Edge que ja esta instalado (channel="msedge") em vez de baixar o Chromium.
A sessao e criada direto no banco com criar_sessao() — o app guarda sessao como
token em tabela, entao nao e preciso senha de ninguem.
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath('.'))

BASE = 'http://127.0.0.1:14000'
SAIDA = sys.argv[1] if len(sys.argv) > 1 else 'prints'
os.makedirs(SAIDA, exist_ok=True)

from app.seguranca import sessao as sessao_mod            # noqa: E402
token = sessao_mod.criar_sessao(1)                        # usuario id 1 = admin
print('sessao criada')

from playwright.sync_api import sync_playwright           # noqa: E402

# (rotulo, caminho, largura, altura, acao opcional)
TELAS = [
    ('01-postos-desktop',      '/postos',       1440, 900,  None),
    ('02-dashboard-desktop',   '/dashboard',    1440, 900,  None),
    ('03-config-desktop',      '/configuracao', 1440, 1000, None),
    ('04-config-tab-hover',    '/configuracao', 1440, 700,  'hover-aba'),
    ('05-topbar-dropdown',     '/postos',       1440, 700,  'abrir-menu'),
    ('06-topbar-rolada',       '/configuracao', 1440, 800,  'rolar'),
    ('07-config-tooltip',      '/configuracao', 1440, 800,  'tooltip'),
    ('08-historico-desktop',   '/historico',    1440, 900,  None),
    ('09-listas-desktop',      '/listas',       1440, 700,  None),
    ('10-documentacao',        '/documentacao', 1440, 950,  None),
    ('11-setup-wizard',        '/setup',        1440, 950,  None),
    ('12-usuarios',            '/usuarios',     1440, 700,  None),
    ('13-cameras',             '/cameras',      1440, 800,  None),
    ('14-auditoria',           '/auditoria',    1440, 800,  None),
    ('15-minha-conta',         '/minha-conta',  1440, 700,  None),
    # ── tablet: iPad em retrato, a faixa que estava quebrada ──
    ('20-postos-tablet-768',   '/postos',        768, 1024, None),
    ('21-config-tablet-768',   '/configuracao',  768, 1024, None),
    ('22-historico-tablet-768', '/historico',    768, 1024, None),
    ('23-dashboard-tablet-768', '/dashboard',    768, 1024, None),
    ('24-postos-celular-390',  '/postos',        390, 844,  None),
    # ── login (sem sessao, mas a pagina nao exige) ──
    ('30-login',               '/login',        1440, 800,  None),
]

with sync_playwright() as pw:
    nav = pw.chromium.launch(channel='msedge', headless=True)
    ctx = nav.new_context(viewport={'width': 1440, 'height': 900},
                          device_scale_factor=1, locale='pt-BR')
    ctx.add_cookies([{'name': 'sessao', 'value': token,
                      'domain': '127.0.0.1', 'path': '/'}])
    pg = ctx.new_page()

    erros = []
    pg.on('console', lambda m: erros.append(f'{m.type}: {m.text}')
          if m.type == 'error' else None)
    pg.on('pageerror', lambda e: erros.append(f'pageerror: {e}'))

    for rotulo, caminho, w, h, acao in TELAS:
        del erros[:]
        pg.set_viewport_size({'width': w, 'height': h})
        try:
            pg.goto(BASE + caminho, wait_until='domcontentloaded', timeout=20000)
        except Exception as e:
            print(f'  FALHOU {rotulo}: {e}')
            continue
        pg.wait_for_timeout(1400)          # deixa o fetch das tabelas voltar

        try:
            if acao == 'abrir-menu':
                pg.click('.nav-menu-trigger')
                pg.wait_for_timeout(350)
                # deixa o mouse sobre um item, que e o bug relatado
                pg.hover('.nav-menu-itens a:nth-of-type(3)')
                pg.wait_for_timeout(250)
            elif acao == 'hover-aba':
                pg.hover('.cfg-tab[data-tab="deteccao"]')
                pg.wait_for_timeout(300)
            elif acao == 'rolar':
                pg.evaluate('window.scrollTo(0, 420)')
                pg.wait_for_timeout(450)
            elif acao == 'tooltip':
                pg.click('.cfg-tab[data-tab="deteccao"]')
                pg.wait_for_timeout(250)
                pg.evaluate("""() => {
                    const h = document.querySelector('#tab-deteccao .help');
                    if (h) { h.scrollIntoView({block:'center'}); h.click(); }
                }""")
                pg.wait_for_timeout(400)
        except Exception as e:
            print(f'  aviso: acao "{acao}" em {rotulo} falhou: {e}')

        pg.screenshot(path=os.path.join(SAIDA, rotulo + '.png'), full_page=False)
        msg = f'  {rotulo:26s} {w}x{h}  {pg.url.replace(BASE, "")}'
        if erros:
            msg += '   [JS: ' + ' | '.join(erros[:2])[:130] + ']'
        print(msg)

    nav.close()

sessao_mod.remover_sessao(token)
print('sessao removida. prints em', os.path.abspath(SAIDA))
