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
