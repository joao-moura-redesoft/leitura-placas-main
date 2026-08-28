"""Nenhum link de imagem sai no payload entregue ao integrador.

A foto da leitura é do posto, e quem a vê é quem entra no sistema web. O sidecar que
consome `GET /api/leitura` recebe a placa e os números que a sustentam.

Este arquivo existe porque a remoção é fácil de desfazer sem querer: `ler_placa` CONTINUA
produzindo `snapshot`/`frame_url` (o botão "Testar como o roteador" precisa deles), então
qualquer refatoração que volte a repassar o retorno cru para a rota reabre o vazamento sem
quebrar nenhum outro teste — `test_payload_leitura.py` congela justamente o contrato COM
as imagens, uma camada abaixo.

As asserções nomeiam as chaves literalmente em vez de derivá-las de `CHAVES_IMAGEM`. Uma
asserção escrita como `set(saida) == set(entrada) - set(CHAVES_IMAGEM)` reimplementaria a
regra que ela deveria verificar, e passaria mesmo com a constante esvaziada.
"""
from __future__ import annotations

from app.web.leitura import _sem_imagens


class TestSemImagens:
    def test_tira_snapshot_e_frame_url_do_topo(self):
        saida = _sem_imagens({
            "placa": "PGK2D93", "confirmada": True,
            "snapshot": "/static/snapshots/20260721T185912_PGK2D93.jpg",
            "frame_url": "/api/bicos/2/preview.jpg",
        })
        assert "snapshot" not in saida
        assert "frame_url" not in saida

    def test_tira_o_frame_url_de_dentro_de_cada_fonte(self):
        """O bico de DUAS câmeras é o caso com mais imagens no payload: além do link do
        topo, cada item de `fontes[]` traz o quadro da sua própria câmera. Tirar só o de
        cima deixaria os dois links de câmera expostos."""
        saida = _sem_imagens({
            "placa": "PGK2D93",
            "frame_url": "/api/bicos/2/preview.jpg",
            "fontes": [
                {"camera_id": 3, "papel": "traseira", "estado": "usada",
                 "frame_url": "/api/bicos/2/preview.jpg?camera_id=3"},
                {"camera_id": 4, "papel": "dianteira", "estado": "usada",
                 "frame_url": "/api/bicos/2/preview.jpg?camera_id=4"},
            ],
        })
        assert all("frame_url" not in f for f in saida["fontes"])

    def test_o_resto_da_fonte_sobrevive(self):
        """Diagnóstico de bico com duas câmeras continua servindo: o que sai é a imagem,
        não a informação de qual câmera votou e por quê."""
        saida = _sem_imagens({
            "fontes": [{"camera_id": 3, "papel": "traseira", "estado": "descartada",
                        "motivo": "sem quadro", "tentativas": 4, "frame_url": "/x.jpg"}],
        })
        fonte = saida["fontes"][0]
        assert fonte == {"camera_id": 3, "papel": "traseira", "estado": "descartada",
                         "motivo": "sem quadro", "tentativas": 4}

    def test_o_desfecho_sem_placa_tambem_perde_a_imagem(self):
        """O retorno sem placa tem conjunto de chaves PRÓPRIO e também carrega
        `frame_url` — filtrar só o caminho de sucesso deixaria metade do vazamento."""
        saida = _sem_imagens({
            "placa": None, "mensagem": "Nenhuma placa detectada nos frames",
            "bboxes_detectadas": 0, "frame_url": "/api/bicos/2/preview.jpg",
        })
        assert "frame_url" not in saida
        assert saida["mensagem"] == "Nenhuma placa detectada nos frames"
        assert saida["bboxes_detectadas"] == 0

    def test_nao_mexe_no_que_a_integracao_de_fato_usa(self):
        """Regressão de escopo: o filtro tira imagem, não empobrece o payload."""
        entrada = {
            "entidade": "OPCAO", "cnpj": "12345678000111", "automacao": "1", "bico": "1",
            "placa": "PGK2D93", "confirmada": True, "padrao": "mercosul",
            "confianca": 0.91, "acordo": 0.85, "modo": "completo", "avisos": [],
            "veiculo": {"consulta": "ok", "combustivel": "Alcool / Gasolina"},
            "snapshot": "/static/snapshots/x.jpg", "frame_url": "/api/bicos/2/preview.jpg",
        }
        saida = _sem_imagens(entrada)
        for chave in ("entidade", "cnpj", "automacao", "bico", "placa", "confirmada",
                      "padrao", "confianca", "acordo", "modo", "avisos", "veiculo"):
            assert saida[chave] == entrada[chave]

    def test_nao_muta_o_retorno_de_ler_placa(self):
        """O painel usa o MESMO `ler_placa` para o botão "Testar como o roteador", e lá as
        imagens são o ponto: é o que o operador olha para ajustar o enquadramento. Se este
        filtro mutasse os dicts que recebe, cortaria a imagem de quem tem direito a ela."""
        fonte = {"camera_id": 3, "frame_url": "/api/bicos/2/preview.jpg?camera_id=3"}
        resultado = {"placa": "PGK2D93", "snapshot": "/static/snapshots/x.jpg",
                     "frame_url": "/api/bicos/2/preview.jpg", "fontes": [fonte]}
        _sem_imagens(resultado)
        assert resultado["snapshot"] == "/static/snapshots/x.jpg"
        assert resultado["frame_url"] == "/api/bicos/2/preview.jpg"
        assert fonte["frame_url"] == "/api/bicos/2/preview.jpg?camera_id=3"

    def test_fontes_ausente_ou_estranho_nao_quebra(self):
        """`fontes` não existe no payload de origens antigas, e um terceiro campo com o
        mesmo nome não pode derrubar a entrega da placa."""
        assert _sem_imagens({"placa": "PGK2D93"}) == {"placa": "PGK2D93"}
        assert _sem_imagens({"fontes": None})["fontes"] is None
