# Identidade Visual do ALPR Redesoft

Sistema de leitura de placas desenvolvido pela Redesoft Sistemas.
Este documento define a paleta de cores, tipografia e padrões de UI a serem aplicados no sistema ALPR, alinhados com a identidade da Redesoft.

---

## 1. Referência de Marca

| Atributo | Valor |
|----------|-------|
| **Empresa** | Redesoft Sistemas |
| **Produto** | ALPR, o Sistema de Leitura de Placas |
| **Tagline Redesoft** | SIMPLICIDADE · SEGURANÇA · AGILIDADE |
| **Personalidade** | Confiante, moderno, acessível, orientado a resultados |
| **Público-alvo** | Operadores de posto, técnicos, gestores e sistemas ERP/PDV |

---

## 2. Paleta de Cores

### Cores Primárias

| Token | Hex | Uso |
|-------|-----|-----|
| `--brand-primary` | `#16a34a` | Botões principais, CTA, status ativo, borda de placa detectada |
| `--brand-primary-hover` | `#15803d` | Hover de botão primário |
| `--brand-dark` | `#071f10` | Fundo de seções escuras, modo noturno, overlay de câmera |
| `--brand-deep` | `#0b3d1e` | Gradiente escuro em cards de câmera |

### Escala Verde (brand-*)

| Token | Hex | Uso |
|-------|-----|-----|
| `--brand-50` | `#f0fdf4` | Fundo de seções claras, tint de inputs |
| `--brand-100` | `#dcfce7` | Badges, chips de status |
| `--brand-200` | `#bbf7d0` | Bordas de hover em inputs |
| `--brand-300` | `#86efac` | Texto claro em fundos escuros |
| `--brand-400` | `#4ade80` | Indicadores de atividade, pulse animado |
| `--brand-500` | `#16a34a` | **Cor primária** |
| `--brand-600` | `#15803d` | Hover de primário |
| `--brand-700` | `#166534` | Links de texto |
| `--brand-800` | `#14532d` | Gradiente final de cards |
| `--brand-900` | `#0b3d1e` | Fundo de cards escuros |
| `--brand-950` | `#071f10` | **Fundo escuro principal** |

### Cores de Acento e Estado

| Token | Hex | Uso |
|-------|-----|-----|
| `--accent-emerald` | `#10b981` | Destaques de dados, indicadores de feed ao vivo |
| `--accent-teal` | `#0d9488` | Gradientes de seção de features |
| `--status-ok` | `#16a34a` | Status OK / câmera operacional |
| `--status-warn` | `#f59e0b` | Sem frame / aguardando / aviso |
| `--status-error` | `#dc2626` | Câmera parada / placa em lista negra |
| `--status-info` | `#2563eb` | Informação / placa Mercosul badge |

### Cores Neutras

| Token | Hex | Uso |
|-------|-----|-----|
| `--bg-page` | `#ffffff` | Fundo de página (modo claro) |
| `--bg-tint` | `#f0fdf4` | Tint verde suave em seções |
| `--bg-card` | `#ffffff` | Fundo de cards |
| `--border-card` | `#e5e7eb` | Borda de cards (`gray-200`) |
| `--text-primary` | `#071f10` | Texto principal em fundo claro |
| `--text-secondary` | `#6b7280` | Texto secundário, captions (`gray-500`) |
| `--text-muted` | `#9ca3af` | Placeholder, texto fraco (`gray-400`) |
| `--text-on-dark` | `#ffffff` | Texto em fundos escuros |
| `--text-on-dark-muted` | `#86efac` | Texto secundário em fundos escuros (`brand-300`) |

### Variáveis CSS (copiar em `:root`)

```css
:root {
  --brand-primary:       #16a34a;
  --brand-primary-hover: #15803d;
  --brand-dark:          #071f10;
  --brand-deep:          #0b3d1e;
  --brand-50:            #f0fdf4;
  --brand-100:           #dcfce7;
  --brand-300:           #86efac;
  --brand-400:           #4ade80;
  --brand-500:           #16a34a;
  --brand-950:           #071f10;

  --accent-emerald:      #10b981;
  --status-ok:           #16a34a;
  --status-warn:         #f59e0b;
  --status-error:        #dc2626;
  --status-info:         #2563eb;

  --bg-page:             #ffffff;
  --bg-tint:             #f0fdf4;
  --bg-card:             #ffffff;
  --border-card:         #e5e7eb;
  --text-primary:        #071f10;
  --text-secondary:      #6b7280;
  --text-muted:          #9ca3af;
  --text-on-dark:        #ffffff;
  --text-on-dark-muted:  #86efac;

  /* Glow verde — usado em tiles de câmera ativa, botões CTA */
  --glow-brand:          0 0 60px rgba(22, 163, 74, 0.30),
                         0 0 120px rgba(22, 163, 74, 0.15);
}
```

---

## 3. Tipografia

A família Redesoft usa **Chalet** (House Industries), uma sans-serif geométrica vintage-moderna com caracteres compactos e arredondados que transmite acessibilidade e confiança.

| Papel | Fonte | Peso | Uso |
|-------|-------|------|-----|
| Títulos / Brand | Chalet New York Nineteen Seventy | 700 a 800 | H1, nome do sistema, headings de seção |
| UI / Corpo | Chalet Paris Nineteen Seventy | 400 a 600 | Labels, botões, textos de interface |
| Fallback | `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif` | — | Quando Chalet não estiver disponível |
| Código / Placas | `'Consolas', 'SFMono-Regular', monospace` | 700 | Exibição de placas detectadas, coordenadas, JSON |

### Escala tipográfica sugerida

| Elemento | Tamanho | Peso | Letra |
|----------|---------|------|-------|
| Nome do sistema (hero) | 2.5rem a 4rem | 800 | normal |
| Título de seção | 1.75rem | 700 | normal |
| Card title | 1.125rem | 600 | normal |
| Body | 0.9375rem | 400 | normal |
| Label / badge | 0.75rem | 600 | `0.05em` (tracking) |
| **Placa detectada** | 1.75rem a 2rem | 700 | `5px` (monospace) |
| Coordenadas / código | 0.75rem | 400 | monospace |

---

## 4. Componentes de UI

### Botões

```css
/* Primário — pill verde */
.btn-primary {
  background: #16a34a;
  color: #ffffff;
  border-radius: 9999px;
  padding: 0.625rem 1.5rem;
  font-weight: 600;
  font-size: 0.875rem;
  border: none;
  transition: background 200ms, box-shadow 200ms, transform 100ms;
}
.btn-primary:hover {
  background: #15803d;
  box-shadow: 0 4px 14px rgba(22, 163, 74, 0.35);
}
.btn-primary:active { transform: scale(0.98); }

/* Secundário — contorno verde */
.btn-secondary {
  background: transparent;
  color: #16a34a;
  border: 2px solid #16a34a;
  border-radius: 9999px;
  padding: 0.5rem 1.5rem;
  font-weight: 600;
  transition: all 200ms;
}
.btn-secondary:hover {
  background: #16a34a;
  color: #ffffff;
}

/* Ghost — fundo escuro */
.btn-ghost-dark {
  background: rgba(255, 255, 255, 0.08);
  color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 9999px;
  padding: 0.5rem 1.25rem;
  font-weight: 500;
  transition: background 200ms;
}
.btn-ghost-dark:hover {
  background: rgba(255, 255, 255, 0.14);
}
```

### Cards

```css
.card {
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 1rem;       /* 16px */
  padding: 1.5rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  transition: transform 400ms ease, box-shadow 400ms ease;
}
.card:hover {
  transform: translateY(-4px);
  box-shadow: 0 20px 40px rgba(0,0,0,0.10);
}

/* Card de câmera — fundo escuro */
.card-camera {
  background: linear-gradient(135deg, #0b3d1e, #071f10);
  border: 1px solid rgba(22, 163, 74, 0.20);
  border-radius: 1rem;
  overflow: hidden;
}
.card-camera.active {
  border-color: #16a34a;
  box-shadow: var(--glow-brand);
}
```

### Navbar

```css
.navbar {
  position: fixed;
  top: 0;
  width: 100%;
  height: 4rem;
  background: transparent;
  transition: background 300ms, box-shadow 300ms;
  z-index: 50;
}
.navbar.scrolled {
  background: rgba(255, 255, 255, 0.98);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border-bottom: 1px solid #f3f4f6;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
```

### Badge de status (câmeras / placas)

```css
/* Use classes conforme o estado */
.badge { border-radius: 9999px; padding: 2px 10px; font-size: 0.75rem; font-weight: 600; }
.badge-ok       { background: #dcfce7; color: #15803d; }
.badge-warn     { background: #fef3c7; color: #b45309; }
.badge-error    { background: #fee2e2; color: #b91c1c; }
.badge-mercosul { background: #dbeafe; color: #1d4ed8; }
.badge-antigo   { background: #f3f4f6; color: #374151; }
```

### Placa detectada

```css
.placa-display {
  font-family: 'Consolas', 'SFMono-Regular', monospace;
  font-size: 2rem;
  font-weight: 700;
  letter-spacing: 5px;
  color: #071f10;
  background: #f0fdf4;
  border: 2px solid #16a34a;
  border-radius: 0.5rem;
  padding: 0.5rem 1.25rem;
  display: inline-block;
}

/* Em fundo escuro */
.placa-display.dark-bg {
  color: #ffffff;
  background: rgba(22, 163, 74, 0.12);
  border-color: #4ade80;
}
```

---

## 5. Padrões de Layout

### Seções

| Tipo | Background | Heading | Body |
|------|-----------|---------|------|
| Claro | `#ffffff` | `#071f10` | `#374151` |
| Tintado | `#f0fdf4` | `#071f10` | `#374151` |
| Escuro | `#071f10` | `#ffffff` | `#86efac` |
| Gradiente card | `linear-gradient(135deg, #0b3d1e, #071f10)` | `#ffffff` | `#86efac` |

### Hero da câmera / dashboard escuro

```css
.hero-dark {
  background: linear-gradient(180deg,
    rgba(7, 31, 16, 0.85),
    rgba(7, 31, 16, 0.60) 40%,
    rgba(7, 31, 16, 0.80)
  );
}
```

### Grid de câmeras

```css
.grid-cameras {
  display: grid;
  gap: 1rem;
  grid-template-columns: 1fr;
}
@media (min-width: 640px)  { .grid-cameras { grid-template-columns: repeat(2, 1fr); } }
@media (min-width: 1024px) { .grid-cameras { grid-template-columns: repeat(3, 1fr); } }
```

---

## 6. Ícones e Indicadores de Atividade

### Pulse de câmera ao vivo

```css
/* Círculo pulsante vermelho — indica câmera ativa gravando */
.live-indicator {
  width: 10px;
  height: 10px;
  background: #dc2626;
  border-radius: 50%;
  animation: pulse-live 1.5s ease-in-out infinite;
}

@keyframes pulse-live {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%       { opacity: 0.5; transform: scale(1.4); }
}

/* Alternativa verde — câmera OK */
.live-indicator.ok { background: #16a34a; }
```

### Indicador de detecção recente

```css
.detection-flash {
  animation: flash-green 0.6s ease-out;
}
@keyframes flash-green {
  0%   { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.7); }
  100% { box-shadow: 0 0 0 20px rgba(22, 163, 74, 0); }
}
```

---

## 7. Identidade Aplicada ao ALPR

### Naming e contexto

| Elemento | Texto |
|----------|-------|
| Nome do produto | **ALPR** · Sistema de Leitura de Placas |
| Sub-nome | Redesoft ALPR |
| Rodapé | © 2026 Redesoft Sistemas · Todos os direitos reservados |
| Página de login | "Bem-vindo ao ALPR" + logo Redesoft |

### Escurecimento natural para interfaces de câmera

O ALPR exibe feeds de vídeo 24 h, e interfaces escuras (fundo `#071f10`) são preferidas para não causar fadiga visual e manter contraste dos bounding boxes verdes. Seções de configuração e histórico podem usar fundo claro (`#ffffff`) seguindo o padrão das páginas informativas do site.

### Mapa de cores por contexto

| Contexto | Background | Borda/Acento | Texto |
|----------|-----------|-------------|-------|
| Dashboard / feeds ao vivo | `#071f10` | `#16a34a` | `#ffffff` / `#86efac` |
| Telas de configuração | `#ffffff` / `#f0fdf4` | `#16a34a` | `#071f10` |
| Painel de saúde | `#ffffff` cards | status-ok/warn/error | `#071f10` |
| Placa detectada com sucesso | fundo claro | `#16a34a` | `#071f10` (monospace) |
| Placa em lista negra | fundo `#fee2e2` | `#dc2626` | `#b91c1c` |
| Placa em lista branca | fundo `#dcfce7` | `#16a34a` | `#15803d` |
| Histórico / tabela | `#ffffff` | zebra `#f9fafb` | `#374151` |
| Login page | `#071f10` (full) | `#16a34a` | `#ffffff` |

---

## 8. Exemplo de Aplicação: Card de Câmera

```html
<div class="card-camera active">
  <div style="position:relative">
    <img src="/stream/1.mjpg" style="width:100%;border-radius:0.5rem">
    <span class="live-indicator ok" style="position:absolute;top:8px;left:8px"></span>
    <span style="position:absolute;top:6px;right:8px;
                 background:rgba(7,31,16,0.7);color:#86efac;
                 font-size:11px;padding:2px 8px;border-radius:9999px;font-weight:600">
      Bomba 1 · Lado A
    </span>
  </div>
  <div style="padding:12px 0 4px;display:flex;align-items:center;gap:10px">
    <span class="placa-display dark-bg">ABC1D23</span>
    <span class="badge badge-mercosul">mercosul</span>
    <span style="color:#86efac;font-size:12px;margin-left:auto">87% confiança</span>
  </div>
</div>
```

---

## Referências

- Site da Redesoft: [https://siteteste.b2click.com/](https://siteteste.b2click.com/) (test build)
- Tipografia: [Chalet, da House Industries](https://houseind.com/hi/chalet)
- Framework base: Tailwind CSS v3 (paleta customizada no site de origem)
