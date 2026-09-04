# -*- coding: utf-8 -*-
"""Mede contraste e overflow no DOM RENDERIZADO (nao no CSS de origem).

Percorre todo texto visivel das telas, resolve a cor de fundo efetiva subindo a
arvore, e reporta o que reprova WCAG AA. Tambem checa overflow horizontal do
documento e alvos de toque pequenos.
"""
import io, os, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.abspath('.'))
BASE = 'http://127.0.0.1:14000'

from app.seguranca import sessao as sessao_mod            # noqa: E402
token = sessao_mod.criar_sessao(1)

from playwright.sync_api import sync_playwright           # noqa: E402

JS = r"""
() => {
  const par = c => { const m = c.match(/[\d.]+/g).map(Number); return m; };
  const lum = ([r,g,b]) => {
    const f = v => { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); };
    return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b);
  };
  const cr = (a,b) => { const la=lum(a), lb=lum(b);
    return (Math.max(la,lb)+0.05)/(Math.min(la,lb)+0.05); };

  // sobe a arvore ate achar um fundo opaco
  const fundo = el => {
    let n = el;
    while (n && n !== document.documentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      const p = par(bg);
      if (p.length >= 3 && (p[3] === undefined || p[3] > 0.85)) return p.slice(0,3);
      n = n.parentElement;
    }
    return [255,255,255];
  };

  const ruins = [], pequenos = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.15) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;

    // alvo de toque
    if (['BUTTON','A','INPUT','SELECT'].includes(el.tagName) &&
        cs.position !== 'absolute' && (r.width < 22 || r.height < 22)) {
      pequenos.push({ tag: el.tagName, cls: (el.className||'').toString().slice(0,30),
                      txt: (el.textContent||'').trim().slice(0,20),
                      w: Math.round(r.width), h: Math.round(r.height) });
    }

    // texto direto deste elemento
    const proprio = [...el.childNodes]
      .filter(n => n.nodeType === 3 && n.textContent.trim())
      .map(n => n.textContent.trim()).join(' ');
    if (!proprio) continue;

    const fg = par(cs.color).slice(0,3);
    const bg = fundo(el);
    const ratio = cr(fg, bg);
    const px = parseFloat(cs.fontSize);
    const peso = parseInt(cs.fontWeight) || 400;
    const grande = px >= 24 || (px >= 18.66 && peso >= 700);
    const min = grande ? 3.0 : 4.5;
    if (ratio < min) {
      ruins.push({ txt: proprio.slice(0,58), ratio: +ratio.toFixed(2), min,
                   px: +px.toFixed(1), fg: cs.color, bg: 'rgb('+bg.join(',')+')',
                   sel: el.tagName.toLowerCase() + (el.className ?
                        '.' + el.className.toString().trim().split(/\s+/).join('.') : '') });
    }
  }
  return {
    ruins, pequenos,
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 1,
    scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
  };
}
"""

TELAS = ['/postos', '/dashboard', '/configuracao', '/historico', '/listas',
         '/documentacao', '/setup', '/usuarios', '/cameras', '/auditoria',
         '/minha-conta', '/entidades', '/empresas', '/automacoes', '/testes',
         '/posto/1', '/posto/novo', '/roi/3']
LARGURAS = [(1440, 900)]

with sync_playwright() as pw:
    nav = pw.chromium.launch(channel='msedge', headless=True)
    ctx = nav.new_context(viewport={'width': 1440, 'height': 900}, locale='pt-BR')
    ctx.add_cookies([{'name': 'sessao', 'value': token,
                      'domain': '127.0.0.1', 'path': '/'}])
    pg = ctx.new_page()

    total_ruins = 0
    total_over = []
    vistos = set()
    for w, h in LARGURAS:
        pg.set_viewport_size({'width': w, 'height': h})
        print('\n' + '=' * 78)
        print('VIEWPORT %dx%d' % (w, h))
        print('=' * 78)
        for caminho in TELAS:
            try:
                pg.goto(BASE + caminho, wait_until='domcontentloaded', timeout=20000)
                pg.wait_for_timeout(1200)
                d = pg.evaluate(JS)
            except Exception as e:
                print('  %-16s ERRO %s' % (caminho, str(e)[:60]))
                continue

            marca = ''
            if d['overflowX']:
                marca = '  <<< OVERFLOW X: scrollWidth %d > %d' % (d['scrollW'], d['innerW'])
                total_over.append('%s @%dpx' % (caminho, w))
            print('  %-16s contraste: %2d falha(s)   toque<22px: %2d%s'
                  % (caminho, len(d['ruins']), len(d['pequenos']), marca))
            total_ruins += len(d['ruins'])
            for r in d['ruins'][:8]:
                k = (caminho, r['txt'], r['ratio'])
                if k in vistos:
                    continue
                vistos.add(k)
                print('       %4.2f:1 (min %.1f) %5.1fpx  %-30s  "%s"'
                      % (r['ratio'], r['min'], r['px'], r['sel'][:30], r['txt']))

    nav.close()

sessao_mod.remover_sessao(token)
print('\n' + '=' * 78)
print('TOTAL de falhas de contraste: %d' % total_ruins)
print('Telas com overflow horizontal: %s' % (', '.join(total_over) or 'nenhuma'))
