# -*- coding: utf-8 -*-
"""Alvos de toque, medidos com tres correcoes em relacao a primeira passada:

1. So mede o que esta DENTRO do viewport. As miniaturas do historico usam
   `loading="lazy"`: fora da tela a <img> nao tem largura natural e o <button>
   colapsa — o que virava "alvo de 2px" que nao existe na pratica.
2. Espera as imagens visiveis terminarem de carregar antes de medir.
3. Para input dentro de <label>, o alvo REAL e o label (clicar nele alterna o
   controle), entao mede o retangulo do label.

Limiar: WCAG 2.5.8 "Target Size (Minimum)", nivel AA = 24x24 CSS px, com a
excecao de "inline" (alvo dentro de uma frase) reportada separadamente.
"""
import io
import os
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.abspath('.'))
BASE = 'http://127.0.0.1:14000'

from app.seguranca import sessao as sessao_mod            # noqa: E402
token = sessao_mod.criar_sessao(1)

from playwright.sync_api import sync_playwright           # noqa: E402

JS = r"""
() => {
  const out = [];
  const sel = 'button, a[href], input, select, textarea, [onclick], [role="button"]';
  const vh = window.innerHeight, vw = window.innerWidth;

  for (const el of document.querySelectorAll(sel)) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (el.type === 'hidden') continue;

    // o alvo real de um input dentro de <label> e o label
    let alvo = el;
    if (['INPUT'].includes(el.tagName)) {
      const lab = el.closest('label');
      if (lab) alvo = lab;
    }
    let r = alvo.getBoundingClientRect();
    if (!r.width || !r.height) continue;

    // so o que esta no viewport (o que o dedo pode alcancar agora)
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;

    // imagem ainda carregando falsifica o tamanho do botao
    const img = el.querySelector && el.querySelector('img');
    if (img && !img.complete) continue;

    const menor = Math.min(r.width, r.height);
    if (menor >= 24) continue;

    // excecao "inline" da WCAG 2.5.8: alvo dentro de um bloco de texto corrido
    const pai = el.parentElement;
    const textoDoPai = pai ? (pai.textContent || '').trim().length : 0;
    const textoDoAlvo = (el.textContent || '').trim().length;
    const inline = cs.display.startsWith('inline') && textoDoPai > textoDoAlvo + 25;

    const cls = (el.className || '').toString().trim().split(/\s+/)
                  .filter(Boolean).slice(0, 3).join('.');
    out.push({
      assinatura: el.tagName.toLowerCase() + (el.type ? '[' + el.type + ']' : '')
                  + (cls ? '.' + cls : '')
                  + (alvo !== el ? ' (alvo=label)' : ''),
      w: Math.round(r.width), h: Math.round(r.height),
      inline: inline,
      txt: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24),
      onde: el.closest('td') ? 'td' : el.closest('nav') ? 'nav' : 'card',
    });
  }
  return out;
}
"""

TELAS = ['/historico', '/dashboard', '/testes', '/posto/1', '/cameras', '/postos',
         '/empresas', '/automacoes', '/configuracao', '/listas', '/usuarios',
         '/auditoria', '/roi/3', '/minha-conta', '/entidades', '/posto/novo']

with sync_playwright() as pw:
    nav = pw.chromium.launch(channel='msedge', headless=True)
    ctx = nav.new_context(viewport={'width': 768, 'height': 1024}, locale='pt-BR')
    ctx.add_cookies([{'name': 'sessao', 'value': token,
                      'domain': '127.0.0.1', 'path': '/'}])
    pg = ctx.new_page()

    geral, amostra = Counter(), {}
    tot_real = tot_inline = 0
    for caminho in TELAS:
        try:
            pg.goto(BASE + caminho, wait_until='domcontentloaded', timeout=25000)
            pg.wait_for_timeout(2200)
            alvos = pg.evaluate(JS)
        except Exception as e:
            print('%-16s ERRO %s' % (caminho, str(e)[:50]))
            continue

        reais = [a for a in alvos if not a['inline']]
        inlines = [a for a in alvos if a['inline']]
        tot_real += len(reais)
        tot_inline += len(inlines)
        print('%-16s reprova AA: %2d   (+%d com excecao inline)'
              % (caminho, len(reais), len(inlines)))
        for a in reais:
            k = a['assinatura'] + ' @' + a['onde']
            geral[k] += 1
            amostra.setdefault(k, (a['w'], a['h'], a['txt']))

    nav.close()

sessao_mod.remover_sessao(token)
print('\n' + '=' * 78)
print('COMPONENTES QUE REPROVAM AA (24px), sem a excecao inline')
print('=' * 78)
for k, n in geral.most_common(20):
    w, h, txt = amostra[k]
    print('  %3dx  %-48s %3dx%-3d  "%s"' % (n, k[:48], w, h, txt))
print('\nTOTAL reprovando AA: %d   |   com excecao inline: %d' % (tot_real, tot_inline))
