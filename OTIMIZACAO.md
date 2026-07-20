# Otimização do Pipeline ALPR

Documento vivo — registra gargalos identificados, impacto estimado e estratégias de melhoria.
Atualizar sempre que um gargalo for resolvido ou um novo for descoberto.

---

## Índice

1. [Estado atual](#1-estado-atual)
2. [Gargalos identificados](#2-gargalos-identificados)
3. [Roadmap de melhorias](#3-roadmap-de-melhorias)
4. [Melhorias já aplicadas](#4-melhorias-já-aplicadas)
5. [Referências e benchmarks](#5-referências-e-benchmarks)

---

## 1. Estado atual

| Métrica                 | Antes (v0)          | Hoje (v2.1)            | Alvo             |
|-------------------------|---------------------|------------------------|------------------|
| CPU (Python, 1 câmera)  | ~60% (spin puro)    | ~8–15%                 | < 10%            |
| RAM total               | ~1,1 GB (EasyOCR)   | ~1,1 GB (EasyOCR)      | < 300 MB         |
| Latência OCR/frame      | 100–900 ms          | 100–900 ms             | < 50 ms          |
| Tempo p/ emitir placa   | 1–3 s               | ~0,6–1 s               | < 0,5 s          |
| Câmeras simultâneas     | 2 testadas          | 2 testadas             | 6 (3 bombas × 2) |
| Precisão OCR Mercosul   | ~40%                | ~92,9%                 | > 95%            |
| Chamadas OCR/placa      | 1 por frame YOLO    | **-80 a -99% (tracker)** | mín. possível  |

Ambiente: Windows 11, Python 3.13, CPU (sem GPU), câmeras Intelbras PoE via RTSP.

> **Maior ganho pendente:** trocar EasyOCR → `fast_plate_ocr` resolve RAM e latência
> de uma vez. É uma linha no `config.txt`.

---

## 2. Gargalos identificados

### 2.1 OCR Engine — maior consumidor de RAM e latência

**Arquivo:** `ocr.py` / `config.py`

O engine padrão `tesseract` faz 1–3 chamadas de processo externo por crop detectado:
```python
for psm in [self.psm, 6, 11]:   # até 3 chamadas de processo se PSM 7 falhar
    texto, conf = self._tentar_psm(pytesseract, img, psm)
```
Cada chamada `pytesseract` faz `subprocess.run(tesseract ...)` — overhead de fork + I/O.

Se o engine configurado for `easyocr`, carrega PyTorch completo (~700–900 MB de RAM) e
faz inferência de rede neural a cada crop (~500 ms em CPU sem GPU).

**Impacto:** RAM (EasyOCR), latência (ambos), CPU (Tesseract subprocess).

**Solução recomendada:** trocar para `fast_plate_ocr`
- Motor ONNX dedicado a placas (sem PyTorch)
- Latência: ~20–50 ms em CPU
- RAM: ~80 MB
- Config: `ocr_engine = fast_plate_ocr`

```
Impacto estimado: RAM -700 MB | Latência OCR -200 ms/detecção
```

---

### 2.2 OCR síncrono no loop principal

**Arquivo:** `pipeline.py` → `_processar_frame`

O OCR roda **dentro** do loop principal. Enquanto o Tesseract processa (100–900 ms), nenhum
frame novo é registrado no estado. O tracker mitiga isso evitando 80–99% das chamadas, mas
quando OCR ocorre, ainda bloqueia.

**Solução:** mover OCR para uma thread worker separada com fila.

```python
class Pipeline:
    def __init__(self, ...):
        self._ocr_queue: queue.Queue = queue.Queue(maxsize=4)
        self._ocr_worker = threading.Thread(target=self._ocr_loop, daemon=True)

    def _processar_frame(self, frame):
        bboxes = self.detector.detectar(frame)
        for bbox in bboxes:
            crop = frame[...]
            try:
                self._ocr_queue.put_nowait((crop, bbox, frame))
            except queue.Full:
                pass  # descarta se fila cheia
```

```
Impacto estimado: elimina bloqueio do loop | stream mais fluido
```

---

### 2.3 JPEG re-encodado por espectador no MJPEG

**Arquivo:** `stream.py`

Cada conexão `/stream/{id}.mjpg` executa `cv2.imencode` independentemente. Com 3 abas × 6
câmeras × 15 fps = 270 JPEGs/segundo desnecessários.

**Solução:** cache de JPEG por câmera — encoda uma vez, distribui para todos os leitores.

> Nota: o modo HLS (`streaming_modo=hls`) já resolve esse gargalo — O(cameras) encodes em vez
> de O(cameras × viewers). Considere HLS se houver múltiplos viewers simultâneos.

```
Impacto estimado (MJPEG): CPU -N×encode por câmera
```

---

### 2.4 YOLO rodando em resolução máxima da câmera

**Arquivo:** `detector.py` / `pipeline.py`

O YOLO recebe o frame completo e redimensiona internamente para 640×640. Com frame 1280×720
há redimensionamento desnecessário.

**Solução A:** redimensionar frame para 640px antes de passar ao detector.

**Solução B (config):** usar sub-stream da câmera (subtype=1) — já configurado como padrão.

```
Impacto estimado: YOLO -20–40% mais rápido
```

---

### 2.5 Consenso invalidado por leituras instáveis

**Arquivo:** `pipeline.py` → `_tentar_emitir`

Quando OCR retorna texto diferente a cada frame para a mesma placa física, o consenso de 3
nunca fecha. O tracker mitiga isso: vence por votos do track_id, não por frames consecutivos.

**Solução complementar:** aceitar coincidência parcial de prefixo.
```python
def _mesma_placa(a: str, b: str) -> bool:
    return a[:5] == b[:5] if len(a) >= 5 and len(b) >= 5 else a == b
```

```
Impacto estimado: reduz falsos não-emitidos em placas com OCR instável
```

---

### 2.6 Duas aquisições de lock por frame no loop

**Arquivo:** `pipeline.py` + `estado.py`

Por frame processado, o loop faz duas aquisições de lock separadas em `estado.py`.

**Solução:** unificar em `estado.registrar_frames(camera_id, frame)`.

```
Impacto estimado: pequeno, mas escala com número de câmeras
```

---

### 2.7 Otimizações do ONNX Runtime não configuradas

**Arquivo:** `detector.py`

A sessão ONNX é criada com configurações padrão (grafo e multi-threading desativados).

**Solução:**
```python
opts = ort.SessionOptions()
opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
opts.intra_op_num_threads = 2
opts.inter_op_num_threads = 1
```

```
Impacto estimado: YOLO -10–30% mais rápido sem custo
```

---

### 2.8 Reconexão de câmera bloqueia o thread por 30s

**Arquivo:** `pipeline.py` → `_loop`

`time.sleep(30)` dentro do loop ignora `self._parar.is_set()` — pipeline demora 30s para
encerrar quando câmera está tentando reconectar.

**Solução:**
```python
for _ in range(30):
    if self._parar.is_set():
        return
    time.sleep(1)
```

```
Impacto estimado: shutdown passa de 30s para < 1s no pior caso
```

---

### 2.9 Snapshots gravados em disco na thread do pipeline

**Arquivo:** `pipeline.py` → `_tentar_emitir`

`cv2.imwrite` é chamado diretamente na thread do pipeline — I/O bloqueante.

**Solução:** salvar via `ThreadPoolExecutor`.
```python
_snapshot_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="snapshot")
_snapshot_pool.submit(cv2.imwrite, str(caminho), crop, [cv2.IMWRITE_JPEG_QUALITY, q])
```

```
Impacto estimado: elimina stutter de I/O na thread de detecção
```

---

### 2.10 Memória: frame_atual (endpoint legado) duplica buffer

**Arquivo:** `estado.py` + `pipeline.py`

`frame_atual` é atualizado toda iteração mesmo sem consumidores ativos no `/stream.mjpg`
global (legado). Dashboard e streams por câmera usam `frames_cameras[id]`.

**Solução:** remover `registrar_frame(frame)` ou o endpoint legado completamente.

```
Impacto estimado: micro, mas reduz confusão de código
```

---

## 3. Roadmap de melhorias

Ordenado por impacto × facilidade de implementação:

| Prioridade | Item                                | Esforço | RAM     | CPU     | Latência |
|:----------:|-------------------------------------|:-------:|:-------:|:-------:|:--------:|
| 🔴 Alta    | 2.1 — Trocar para `fast_plate_ocr`  | Baixo   | -700 MB | -30%    | -200 ms  |
| 🔴 Alta    | 2.7 — ONNX Runtime session options  | Baixo   | —       | -20%    | -20 ms   |
| 🟡 Média   | 2.3 — Cache JPEG MJPEG / trocar HLS | Médio   | estável | -N×enc  | —        |
| 🟡 Média   | 2.8 — Sleep interrompível p/ parar  | Baixo   | —       | —       | shutdown |
| 🟡 Média   | 2.9 — Snapshot I/O assíncrono       | Baixo   | —       | stutter | —        |
| 🟡 Média   | 2.4 — Frame menor para YOLO         | Médio   | -30%    | -25%    | -20 ms   |
| 🟢 Baixa   | 2.2 — OCR assíncrono (thread queue) | Alto    | —       | distrib | stream   |
| 🟢 Baixa   | 2.5 — Consenso tolerante a OCR      | Baixo   | —       | —       | emit     |
| 🟢 Baixa   | 2.6 — Unificar lock de frame        | Baixo   | —       | micro   | —        |
| 🟢 Baixa   | 2.10 — Remover frame_atual legado   | Baixo   | micro   | micro   | —        |

---

## 4. Melhorias já aplicadas

| Data       | Commit     | Melhoria                                                        |
|------------|------------|-----------------------------------------------------------------|
| 2026-05-18 | `e6610a2`  | `_focar_caracteres`: crop por projeção de pixels nos chars      |
| 2026-05-18 | `c0af4c4`  | Header detection agnóstica a cor (transição escuro→branco)      |
| 2026-05-18 | `6ea4f33`  | Sem expansão de bbox para cima — evita capturar header azul     |
| 2026-05-18 | `5ce56f9`  | Proporções QR/BR corretas baseadas no template oficial          |
| 2026-05-18 | `5360a46`  | Máscara QR/BR só quando header é detectado (não aplica em moto) |
| 2026-05-18 | `ae67503`  | Validador com janela deslizante para texto > 7 chars            |
| 2026-05-18 | `293b012`  | Remove QR code e marcador BR antes do OCR (Mercosul)            |
| 2026-05-18 | `70db3ec`  | Falha rápida se porta em uso — evita pipeline duplo             |
| 2026-05-18 | `8133ace`  | `_loop` com sleep por camera_fps — elimina spin de CPU          |
| 2026-05-18 | `396df45`  | PSM 6 padrão, `frames_consenso` 5→3, retry sem duplicatas      |
| 2026-05-20 | sessão     | Tracker IoU + wrapper ByteTrack — reduz OCR em 80–99%          |
| 2026-05-20 | sessão     | WorkerSupervisor — reinicia pipelines mortos com backoff        |
| 2026-05-20 | sessão     | HLS Streaming via FFmpeg — O(cameras) encodes para N viewers   |
| 2026-05-20 | sessão     | ROI por câmera — elimina detecções fora da área de interesse   |
| 2026-05-20 | sessão     | Painel de saúde no dashboard (`/api/health`)                   |
| 2026-07-20 | sessão     | Deskew rotacional — `minAreaRect` + `warpAffine` antes do OCR  |

---

## 5. Referências e benchmarks

### Comparativo de engines OCR para placas brasileiras (CPU, sem GPU)

| Engine          | RAM       | Latência/crop | Precisão placa BR | Instalação  |
|-----------------|-----------|---------------|-------------------|-------------|
| Tesseract 5     | ~50 MB    | 80–300 ms     | Média (com PSM 6) | apt/winget  |
| EasyOCR         | ~800 MB   | 400–1200 ms   | Alta              | pip         |
| PaddleOCR       | ~400 MB   | 150–400 ms    | Alta              | pip         |
| **fast_plate_ocr** | **~80 MB** | **20–50 ms** | **Alta (dedicado)** | **pip**  |
| docTR           | ~600 MB   | 200–600 ms    | Média-alta        | pip         |

### Benchmark do Tracker (IoU interno, simulação)

| Cenário | Sem tracker | Com tracker | Redução OCR |
|---------|-------------|-------------|-------------|
| 1 veículo, 15 fps, cooldown 30s | 450 frames | ~5 OCR | ~98,9% |
| 3 veículos simultâneos | proporcional | ~15 OCR | ~96,7% |
| Tráfego intenso (5+ veículos) | 675+ frames | ~25 OCR | ~96,3% |

Benchmark médio: **~84,5% de redução** nas configurações padrão (5 fps detecção, 30s cooldown).

### Parâmetros de performance recomendados (`config.txt`)

```ini
ocr_engine         = fast_plate_ocr   # troca mais impactante
deteccao_fps_max   = 3                # 3 detecções/s suficiente para < 30 km/h
frames_consenso    = 3                # confirmação em 1s
camera_fps         = 15               # sub-stream Intelbras
intelbras_subtype  = 1                # sub-stream (menos CPU de decode)
tracker_ativo      = sim              # reduz OCR em 80–99%
tracker_ocr_intervalo = 5             # OCR a cada 5 frames do mesmo veículo
tracker_votos_emitir  = 2             # 2 votos concordantes para emitir
```
