// Helpers globais do layout autenticado (base.html).
// Extraído das ~15 páginas que reimplementavam esc()/api() cada uma à sua
// maneira — inclusive com pequenas divergências entre elas (algumas não
// escapavam aspas, o que é um bug real quando o resultado vai para dentro
// de um atributo HTML tipo value="${esc(x)}"). Esta é a versão canônica.

// Redireciona para /login em qualquer resposta 401 de API — sessão expirada ou inválida.
(function () {
  const _orig = window.fetch;
  window.fetch = async function (...args) {
    const r = await _orig(...args);
    if (r.status === 401) {
      const url = typeof args[0] === 'string' ? args[0] : (args[0].url || '');
      // Não redireciona em chamadas de auth para evitar loop
      if (!url.includes('/login') && !url.includes('/criar-admin')) {
        location.href = '/login';
        return new Promise(() => {});   // interrompe a cadeia de .then()
      }
    }
    return r;
  };
})();

// Escapa texto para uso seguro tanto em conteúdo HTML quanto dentro de
// atributos entre aspas (value="${esc(x)}"), que é como boa parte das
// páginas usam esta função.
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Wrapper padrão de chamada à API JSON usado pelas páginas de cadastro.
async function api(metodo, url, body) {
  const r = await fetch(url, {
    method: metodo,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `${metodo} ${url} -> ${r.status}`);
  return d;
}

// Mensagens de erro/sucesso no lugar de alert() — não bloqueia, cabe texto longo
// (as mensagens de validação do servidor são explicativas) e permite links.
// Visibilidade é por CLASSE, não por style.display: a entrada e a saída são
// animadas em base.css (#aviso-global.visivel / .saindo) e um display direto
// no elemento cortaria a animação pela metade.
function avisar(texto, tipo) {
  const el = document.getElementById('aviso-global');
  const icone = tipo === 'ok'
    ? `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px">
         <path d="M20 6 9 17l-5-5"/></svg>`
    : `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px">
         <circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg>`;

  el.className = 'alert alert-' + (tipo === 'ok' ? 'ok' : 'err');
  el.innerHTML = `<div class="aviso-corpo">${icone}
      <span class="aviso-texto">${texto}</span>
      <button type="button" class="aviso-fechar" aria-label="Fechar aviso"
              onclick="fecharAviso()">✕</button>
    </div>`;

  // Reinicia a animação quando um aviso substitui outro que ainda está na tela:
  // sem remover a classe e forçar reflow, o browser reaproveita o keyframe já
  // concluído e o novo aviso aparece sem movimento nenhum.
  el.classList.remove('visivel', 'saindo');
  void el.offsetWidth;
  el.classList.add('visivel');

  clearTimeout(window.__avisoT);
  window.__avisoT = setTimeout(fecharAviso, tipo === 'ok' ? 4000 : 9000);
}

// Saída animada: marca .saindo, espera o keyframe e só então esconde.
function fecharAviso() {
  const el = document.getElementById('aviso-global');
  if (!el || !el.classList.contains('visivel')) return;
  clearTimeout(window.__avisoT);
  el.classList.add('saindo');
  setTimeout(() => el.classList.remove('visivel', 'saindo'), 260);
}

// Extrai a mensagem do FastAPI e avisa. Uso: if (!res.ok) return avisarResposta(res);
async function avisarResposta(res, prefixo) {
  const d = await res.json().catch(() => ({}));
  avisar((prefixo ? prefixo + ': ' : '') + (d.detail || `erro ${res.status}`), 'err');
}

// Fecha um overlay de modal por id. O corpo — document.getElementById(id)
// .classList.remove('aberto') — estava copiado em 10 funções fecharModal*()
// diferentes (bicos, empresas, usuarios, automacoes, entidades, cameras, posto).
// Cada página mantém sua própria fecharModal() (o nome varia e algumas fazem
// limpeza extra de estado), só o corpo passa a chamar isto.
function fecharOverlay(id) { document.getElementById(id)?.classList.remove('aberto'); }

// Marca o botão como ocupado enquanto a promessa não resolve. Existe porque
// toda ação que chama a câmera ou o OCR leva SEGUNDOS (abrir RTSP, rodar o
// pipeline) e sem retorno visual o operador clica de novo, disparando a mesma
// leitura duas vezes. A classe .carregando também desliga pointer-events.
async function comCarregamento(btn, fn) {
  if (!btn || btn.classList.contains('carregando')) return;
  btn.classList.add('carregando');
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.classList.remove('carregando');
    btn.disabled = false;
  }
}

// Linhas de esqueleto para <tbody> enquanto o fetch não volta. Melhor que uma
// tabela vazia: mantém a altura do card, então a página não salta quando os
// dados chegam — o que importa aqui porque o dashboard recarrega a cada 15s.
function esqueletoLinhas(colunas, linhas = 3) {
  const cel = `<td><div class="esqueleto"></div></td>`;
  return Array.from({ length: linhas }, () => `<tr>${cel.repeat(colunas)}</tr>`).join('');
}

// ── Imagem de câmera ─────────────────────────────────────────────────────────
// Toda tela que mostra câmera (posto, editor de áreas) precisa da MESMA garantia:
// nunca deixar um retângulo vazio. Um <img> apontando para um MJPEG que não emite
// quadro nenhum não dispara `load` nem `error` — fica em branco calado para sempre.
// Por isso todo caminho aqui termina em imagem OU em mensagem de erro visível.

// `capturar_frame_unico` abre o RTSP na hora: em câmera remota leva segundos, e se
// estiver fora do ar o socket pode demorar muito mais. Sem teto, a promessa nunca
// se resolve e a tela fica em "capturando…" indefinidamente.
const SNAPSHOT_TIMEOUT_MS = 45000;
// Tempo até considerar que o MJPEG não vai entregar o primeiro quadro.
const MJPEG_TIMEOUT_MS = 12000;

// Baixa o frame atual da câmera. Devolve uma blob: URL ou levanta Error com o
// motivo já em português, pronto para exibir.
async function capturarSnapshot(cameraId, timeoutMs = SNAPSHOT_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`/api/cameras/${cameraId}/snapshot?t=${Date.now()}`, { signal: ctrl.signal });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `erro ${r.status}`);
    return URL.createObjectURL(await r.blob());
  } catch (ex) {
    if (ex.name === 'AbortError') {
      throw new Error(`A câmera não respondeu em ${Math.round(timeoutMs / 1000)}s — sem imagem.`);
    }
    throw new Error(ex.message || 'Falha ao capturar imagem da câmera.');
  } finally {
    clearTimeout(t);
  }
}

// Troca o src revogando a blob: URL anterior — telas de câmera recapturam sob
// demanda e cada blob órfão segura o JPEG inteiro na memória da aba.
function definirImagem(img, url) {
  const anterior = img.dataset.blobUrl;
  if (anterior) { URL.revokeObjectURL(anterior); delete img.dataset.blobUrl; }
  if (url.startsWith('blob:')) img.dataset.blobUrl = url;
  img.src = url;
}

// Vigia um <img> de MJPEG: chama `aoFalhar(motivo)` se o primeiro quadro não
// chegar a tempo ou se a resposta for erro (o servidor responde 503 quando a
// câmera não está transmitindo). Devolve uma função para cancelar a vigília.
function vigiarMjpeg(img, aoFalhar, timeoutMs = MJPEG_TIMEOUT_MS) {
  let cancelado = false;
  const limpar = () => {
    clearTimeout(t);
    img.removeEventListener('load', aoCarregar);
    img.removeEventListener('error', aoErro);
  };
  const aoCarregar = () => { limpar(); };
  const aoErro = () => {
    limpar();
    if (!cancelado) aoFalhar('A transmissão ao vivo não está disponível.');
  };
  const t = setTimeout(() => {
    limpar();
    if (cancelado) return;
    img.removeAttribute('src');   // encerra a conexão pendurada
    aoFalhar(`A câmera não enviou nenhum quadro em ${Math.round(timeoutMs / 1000)}s.`);
  }, timeoutMs);
  img.addEventListener('load', aoCarregar);
  img.addEventListener('error', aoErro);
  return () => { cancelado = true; limpar(); };
}

// ── Visualizador de imagem ───────────────────────────────────────────────────
// Abrir a imagem da leitura em aba nova entregava um JPEG estático: o zoom é o do
// navegador (afeta a aba inteira), não dá para arrastar e o operador perde a linha
// da tabela em que estava. Este visualizador abre por cima da página, com zoom por
// roda/pinça, arrasto e troca entre o recorte e o quadro da MESMA leitura.
//
// Fica aqui, e não no template do histórico, porque a marcação `<a target="_blank">
// <img>` se repete em outras telas de imagem (testes, posto) — a próxima só precisa
// chamar abrirVisualizador().

// A escala é sempre em relação ao PIXEL ORIGINAL da imagem: 100% é o JPEG no
// tamanho em que foi gravado. O enquadramento inicial ("ajuste") é calculado por
// imagem — um quadro de 1280px entra reduzido, um recorte de placa de 60px entra
// ampliado. Sem isso o recorte, que é o que o operador mais precisa examinar,
// abria como um selo no meio do palco.
const VISU_ZOOM_MAX = 40;     // teto absoluto do zoom manual
const VISU_AJUSTE_MAX = 10;   // teto do enquadramento automático (ver _visuCalcAjuste)
let _visu = null;

// Monta o overlay uma única vez, no primeiro uso. Nenhuma página precisa carregar
// esta marcação: quem nunca clica numa imagem não paga por ela.
function _visuMontar() {
  if (_visu) return _visu;

  const ov = document.createElement('div');
  ov.className = 'visu';
  ov.setAttribute('role', 'dialog');
  ov.setAttribute('aria-modal', 'true');
  ov.setAttribute('aria-label', 'Visualizador de imagem');
  ov.innerHTML = `
    <div class="visu-topo">
      <div>
        <div class="visu-titulo"></div>
        <div class="visu-sub"><span class="visu-contexto"></span> <span class="visu-dim"></span></div>
      </div>
      <div class="visu-ferramentas">
        <button type="button" class="visu-btn" data-acao="menos" title="Reduzir (tecla −)" aria-label="Reduzir">−</button>
        <span class="visu-zoom">100%</span>
        <button type="button" class="visu-btn" data-acao="mais" title="Ampliar (tecla +)" aria-label="Ampliar">+</button>
        <button type="button" class="visu-btn" data-acao="ajustar" title="Voltar ao tamanho da tela (tecla 0)">Ajustar</button>
        <button type="button" class="visu-btn" data-acao="pixels" title="Mostrar os pixels sem suavização">Pixels</button>
        <button type="button" class="visu-btn" data-acao="baixar" title="Baixar a imagem">Baixar</button>
        <button type="button" class="visu-btn" data-acao="aba" title="Abrir em nova aba">Nova aba</button>
        <button type="button" class="visu-btn visu-fechar" data-acao="fechar" title="Fechar (Esc)" aria-label="Fechar">✕</button>
      </div>
    </div>
    <div class="visu-palco">
      <img class="visu-img" alt="">
      <p class="visu-erro" hidden>Não foi possível carregar esta imagem — o arquivo pode ter sido removido pela retenção do posto.</p>
    </div>
    <div class="visu-tira"></div>`;
  document.body.appendChild(ov);

  _visu = {
    ov,
    palco: ov.querySelector('.visu-palco'),
    img:   ov.querySelector('.visu-img'),
    erro:  ov.querySelector('.visu-erro'),
    tira:  ov.querySelector('.visu-tira'),
    itens: [], idx: 0,
    escala: 1, ajuste: 1, tx: 0, ty: 0,
    ponteiros: new Map(),   // pointerId -> {x, y}: um = arrasto, dois = pinça
    pinca: 0,               // distância entre os dois dedos no quadro anterior
    arrastou: false,
  };

  // O ajuste só pode ser calculado com as dimensões reais, que só existem depois
  // do load. Até lá a imagem fica com width/height do natural anterior, por isso
  // ela é escondida em _visuTrocar() e revelada aqui.
  _visu.img.addEventListener('load', () => {
    const v = _visu;
    v.img.style.width = v.img.naturalWidth + 'px';
    v.img.style.height = v.img.naturalHeight + 'px';
    v.img.style.visibility = '';
    v.erro.hidden = true;
    _visuAjustar();
    _visuPixelsAuto();
    ov.querySelector('.visu-dim').textContent = `· ${v.img.naturalWidth}×${v.img.naturalHeight} px`;
  });
  _visu.img.addEventListener('error', () => {
    _visu.img.style.visibility = 'hidden';
    _visu.erro.hidden = false;
  });

  ov.querySelector('.visu-ferramentas').addEventListener('click', e => {
    const acao = e.target.closest('button')?.dataset.acao;
    if (!acao) return;
    const v = _visu, atual = v.itens[v.idx];
    if (acao === 'menos')   _visuZoom(v.escala / 1.4);
    if (acao === 'mais')    _visuZoom(v.escala * 1.4);
    if (acao === 'ajustar') _visuAjustar();
    if (acao === 'pixels')  {
      const on = v.img.classList.toggle('pixels');
      e.target.closest('button').classList.toggle('ativo', on);
    }
    if (acao === 'aba')    window.open(atual.url, '_blank', 'noopener');
    if (acao === 'baixar') {
      const a = document.createElement('a');
      a.href = atual.url;
      a.download = atual.arquivo || (atual.url.split('/').pop() || 'imagem.jpg');
      a.click();
    }
    if (acao === 'fechar') fecharVisualizador();
  });

  // Zoom na roda ancorado no cursor: sem isso, ampliar joga para fora da tela
  // justamente a região que se estava olhando. `passive: false` porque o
  // preventDefault é o que impede a página de rolar por baixo do visualizador.
  _visu.palco.addEventListener('wheel', e => {
    e.preventDefault();
    _visuZoom(_visu.escala * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX, e.clientY);
  }, { passive: false });

  // Duplo clique: aproxima 3x no ponto clicado, ou volta ao enquadramento se já
  // estiver ampliado. Múltiplo do ajuste, não valor fixo — num recorte que abre a
  // 1000%, "ir para 400%" seria afastar.
  _visu.palco.addEventListener('dblclick', e => {
    const v = _visu;
    if (v.escala > v.ajuste * 1.001) _visuAjustar();
    else _visuZoom(v.ajuste * 3, e.clientX, e.clientY);
  });

  _visu.palco.addEventListener('pointerdown', e => {
    const v = _visu;
    v.palco.setPointerCapture(e.pointerId);
    v.ponteiros.set(e.pointerId, { x: e.clientX, y: e.clientY });
    v.arrastou = false;
    v.pinca = 0;
    v.palco.classList.add('arrastando');
  });

  _visu.palco.addEventListener('pointermove', e => {
    const v = _visu;
    const p = v.ponteiros.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) v.arrastou = true;

    if (v.ponteiros.size >= 2) {
      // Pinça: a escala segue a variação da distância entre os dois dedos e o
      // ponto de ancoragem é o meio deles — é o que faz o gesto parecer que
      // está "puxando" a imagem, e não aplicando zoom no centro da tela.
      const [a, b] = [...v.ponteiros.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (v.pinca) _visuZoom(v.escala * (dist / v.pinca), (a.x + b.x) / 2, (a.y + b.y) / 2);
      v.pinca = dist;
      return;
    }
    if (v.escala <= v.ajuste * 1.001) return;   // no enquadramento não há para onde arrastar
    v.tx += dx; v.ty += dy;
    _visuAplicar();
  });

  const soltar = e => {
    const v = _visu;
    if (!v.ponteiros.delete(e.pointerId)) return;
    v.pinca = 0;
    if (!v.ponteiros.size) v.palco.classList.remove('arrastando');
  };
  _visu.palco.addEventListener('pointerup', soltar);
  _visu.palco.addEventListener('pointercancel', soltar);

  // Clique no fundo fecha — mas não quando o ponteiro só terminou um arrasto,
  // senão soltar a imagem fora dela fecharia o visualizador sem querer.
  _visu.palco.addEventListener('click', e => {
    if (e.target !== _visu.palco || _visu.arrastou) return;
    fecharVisualizador();
  });

  document.addEventListener('keydown', e => {
    if (!_visu || !_visu.ov.classList.contains('aberto')) return;
    const teclas = {
      Escape:     () => fecharVisualizador(),
      '+':        () => _visuZoom(_visu.escala * 1.4),
      '=':        () => _visuZoom(_visu.escala * 1.4),
      '-':        () => _visuZoom(_visu.escala / 1.4),
      '0':        () => _visuAjustar(),
      ArrowRight: () => _visuIr(_visu.idx + 1),
      ArrowLeft:  () => _visuIr(_visu.idx - 1),
    };
    if (!teclas[e.key]) return;
    e.preventDefault();
    teclas[e.key]();
  });

  // Girar o tablet muda o palco inteiro e o enquadramento precisa ser recalculado.
  // Mas só REENQUADRA quem ainda estava no enquadramento: se o operador ampliou uma
  // região da placa, jogá-lo de volta para a imagem inteira porque a barra de
  // endereço do navegador recolheu seria perder o que ele estava olhando.
  window.addEventListener('resize', () => {
    const v = _visu;
    if (!v?.ov.classList.contains('aberto') || !v.img.naturalWidth) return;
    const enquadrado = Math.abs(v.escala - v.ajuste) < 0.001;
    v.ajuste = _visuCalcAjuste();
    if (enquadrado) { v.escala = v.ajuste; v.tx = 0; v.ty = 0; }
    _visuAplicar();
  });

  return _visu;
}

// Escala que faz a imagem caber inteira no palco, com uma folga. Pode ser maior
// que 1 (recorte pequeno) ou menor (quadro de 1280px numa janela apertada).
function _visuCalcAjuste() {
  const v = _visu;
  const r = v.palco.getBoundingClientRect();
  const w = v.img.naturalWidth, h = v.img.naturalHeight;
  if (!w || !h) return 1;
  // Teto no enquadramento inicial: um recorte de 56px encheria a tela a 2000%, e o
  // que aparece nessa ampliação é a interpolação, não a placa. 10x já entrega a
  // maior imagem que ainda tem informação; acima disso é escolha do operador.
  return Math.min(VISU_AJUSTE_MAX, (r.width - 32) / w, (r.height - 32) / h);
}

// Escala nova ancorada em (cx, cy) da tela. Com transform "translate() scale()" e
// origem no centro, o ponto sob o cursor só permanece parado se a translação for
// corrigida na mesma proporção da mudança de escala.
function _visuZoom(escala, cx, cy) {
  const v = _visu;
  // Piso no enquadramento (ou em 100%, o que for menor): reduzir além disso só
  // afasta a imagem, não mostra mais nada.
  const min = Math.min(v.ajuste, 1);
  const nova = Math.min(VISU_ZOOM_MAX, Math.max(min, escala));
  const r = v.palco.getBoundingClientRect();
  const ax = (cx ?? r.left + r.width / 2) - (r.left + r.width / 2);
  const ay = (cy ?? r.top + r.height / 2) - (r.top + r.height / 2);
  const k = nova / v.escala;
  v.tx = ax - k * (ax - v.tx);
  v.ty = ay - k * (ay - v.ty);
  v.escala = nova;
  _visuAplicar();
}

function _visuAjustar() {
  const v = _visu;
  v.ajuste = _visuCalcAjuste();
  v.escala = v.ajuste;
  v.tx = 0; v.ty = 0;
  _visuAplicar();
}

// Recorte de placa entra no palco com uns 2000% de ampliação; com interpolação
// isso é um borrão liso, em que não dá para separar 8 de B. Acima de 4x o padrão
// passa a ser mostrar o pixel real. Só na TROCA de imagem — depois disso o botão
// é do operador, e reenquadrar não desfaz a escolha dele.
function _visuPixelsAuto() {
  const v = _visu;
  const on = v.escala > 4;
  v.img.classList.toggle('pixels', on);
  v.ov.querySelector('[data-acao="pixels"]').classList.toggle('ativo', on);
}

// Mantém a imagem presa à tela: passado o limite, o arrasto some com ela e o
// operador fica com um palco preto sem entender que a imagem continua ali.
function _visuAplicar() {
  const v = _visu;
  const r = v.palco.getBoundingClientRect();
  const largura = v.img.naturalWidth * v.escala, altura = v.img.naturalHeight * v.escala;
  const limiteX = Math.max(0, (largura - r.width) / 2 + 40);
  const limiteY = Math.max(0, (altura - r.height) / 2 + 40);
  v.tx = Math.min(limiteX, Math.max(-limiteX, v.tx));
  v.ty = Math.min(limiteY, Math.max(-limiteY, v.ty));
  v.img.style.transform = `translate(${v.tx}px, ${v.ty}px) scale(${v.escala})`;
  // "Ampliado" aqui é em relação ao enquadramento, não a 100%: é o único caso em
  // que sobra imagem fora do palco e o arrasto passa a fazer alguma coisa.
  v.palco.classList.toggle('ampliado', v.escala > v.ajuste * 1.001);
  v.ov.querySelector('.visu-zoom').textContent = Math.round(v.escala * 100) + '%';
}

function _visuIr(i) {
  const v = _visu;
  if (v.itens.length < 2) return;
  v.idx = (i + v.itens.length) % v.itens.length;   // circula: são 2 ou 3 imagens
  _visuTrocar();
}

function _visuTrocar() {
  const v = _visu, item = v.itens[v.idx];
  // Escondida até o load: com as dimensões da imagem ANTERIOR ainda no style, ela
  // apareceria por um quadro esticada no tamanho errado.
  v.img.style.visibility = 'hidden';
  v.erro.hidden = true;
  v.ov.querySelector('.visu-dim').textContent = '';
  v.img.src = item.url;
  v.img.alt = item.rotulo || 'Imagem da leitura';
  v.ov.querySelector('.visu-contexto').textContent =
    [v.legenda, item.rotulo].filter(Boolean).join(' · ');
  v.tira.querySelectorAll('button').forEach((b, i) =>
    b.setAttribute('aria-current', String(i === v.idx)));
  // Imagem em cache dispara `load` antes daqui em alguns navegadores; nesse caso
  // o handler já ajustou. Se não, isto evita ficar com o transform do item anterior.
  if (v.img.complete && v.img.naturalWidth) {
    v.img.style.width = v.img.naturalWidth + 'px';
    v.img.style.height = v.img.naturalHeight + 'px';
    v.img.style.visibility = '';
    v.ov.querySelector('.visu-dim').textContent = `· ${v.img.naturalWidth}×${v.img.naturalHeight} px`;
    _visuAjustar();
    _visuPixelsAuto();
  }
}

// itens: [{ url, rotulo?, arquivo? }]. `titulo`/`legenda` identificam a leitura —
// sem eles o operador que abre a terceira imagem seguida não sabe mais de qual
// linha da tabela ela veio.
function abrirVisualizador(itens, indice = 0, titulo = '', legenda = '') {
  const v = _visuMontar();
  v.itens = (itens || []).filter(i => i && i.url);
  if (!v.itens.length) return;
  v.idx = Math.min(Math.max(indice, 0), v.itens.length - 1);
  v.legenda = legenda;
  v.ov.querySelector('.visu-titulo').textContent = titulo;

  v.tira.innerHTML = v.itens.length > 1
    ? v.itens.map((it, i) => `<button type="button" onclick="_visuIr(${i})">
         <img src="${esc(it.url)}" alt=""><span>${esc(it.rotulo || 'imagem')}</span></button>`).join('')
    : '';
  v.tira.style.display = v.itens.length > 1 ? '' : 'none';

  // Abre ANTES de trocar a imagem: o enquadramento depende do tamanho do palco, e
  // com o overlay ainda em display:none esse tamanho é zero.
  v.ov.classList.add('aberto');
  _visuTrocar();
  // A página por baixo não pode rolar junto: no tablet, o arrasto que sobra do
  // gesto de pan escorregaria para o scroll do histórico.
  document.body.style.overflow = 'hidden';
  v.ov.querySelector('.visu-fechar').focus();
}

function fecharVisualizador() {
  if (!_visu) return;
  _visu.ov.classList.remove('aberto');
  _visu.ponteiros.clear();
  _visu.palco.classList.remove('arrastando');
  _visu.img.removeAttribute('src');   // não segura o JPEG na memória com o visualizador fechado
  document.body.style.overflow = '';
}

// Exclusão em cascata (entidade/posto apaga o cadastro inteiro de um cliente) exige
// digitar o nome. confirm() é um clique de distância de destruir dados de produção.
function confirmarExclusao(nome, oQueApaga) {
  const dig = prompt(
    `Isto apaga ${oQueApaga}.\n\nEsta ação não pode ser desfeita.\n` +
    `Para confirmar, digite o nome exatamente:\n\n${nome}`);
  if (dig === null) return false;
  if (dig.trim() !== String(nome).trim()) {
    avisar('Nome não confere — nada foi apagado.', 'err');
    return false;
  }
  return true;
}

// Menus da barra: clique abre/fecha, clique fora ou Esc fecha.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.nav-menu-trigger').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const menu = btn.closest('.nav-menu');
      const abrindo = !menu.classList.contains('aberto');
      document.querySelectorAll('.nav-menu').forEach(m => {
        m.classList.remove('aberto');
        m.querySelector('.nav-menu-trigger').setAttribute('aria-expanded', 'false');
      });
      if (abrindo) { menu.classList.add('aberto'); btn.setAttribute('aria-expanded', 'true'); }
    });
  });
  const fechar = () => document.querySelectorAll('.nav-menu').forEach(m => {
    m.classList.remove('aberto');
    m.querySelector('.nav-menu-trigger')?.setAttribute('aria-expanded', 'false');
  });
  document.addEventListener('click', fechar);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') fechar(); });

  // Esc e clique no fundo fecham modais do componente novo (.modal-overlay).
  // Escopo restrito a essa classe de propósito: as páginas antigas usam
  // #modal-overlay por id e têm fecharModal() própria com limpeza de estado —
  // fechar por baixo delas deixaria a página com estado sujo.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.modal-overlay.aberto').forEach(m => m.classList.remove('aberto'));
  });
  document.addEventListener('click', e => {
    if (e.target.classList?.contains('modal-overlay')) e.target.classList.remove('aberto');
  });
});
