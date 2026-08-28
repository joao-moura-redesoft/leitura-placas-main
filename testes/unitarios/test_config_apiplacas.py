"""O ciclo de configuração do token da apiplacas pela tela `/configuracao`.

O token é uma credencial PAGA, e o modo de falha desta integração é silencioso: se ela
não estiver de pé, nada quebra — o combustível apenas nunca chega no payload. Por isso o
que se verifica aqui não é só "salvou", e sim que a tela consegue DIZER se o recurso está
funcionando, e que salvar a tela inteira nunca apaga o token por descuido.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import config
from app.web import redacao

HTML = Path("app/web/templates/configuracao.html").read_text(encoding="utf-8")


class TestSegredo:
    def test_token_nunca_sai_em_texto_claro(self, ambiente, admin):
        config.salvar({**config.carregar(), "apiplacas_token": "SEGREDO-PAGO"})
        corpo = admin.get("/api/config").json()
        assert corpo["apiplacas_token"] == redacao.MASCARA
        assert "SEGREDO-PAGO" not in str(corpo)

    def test_salvar_a_tela_inteira_com_mascara_nao_apaga_o_token(self, ambiente, admin):
        """O caso real (achado A7): o admin abre a tela (o campo vem MASCARADO, não mais
        vazio), muda o intervalo de retenção e salva sem tocar no token. O POST leva
        `apiplacas_token: "********"` de volta — e isso NÃO pode zerar a credencial,
        senão a integração cai sem ninguém ter mexido nela.
        """
        config.salvar({**config.carregar(), "apiplacas_token": "SEGREDO-PAGO"})
        r = admin.post("/api/config", json={"apiplacas_token": redacao.MASCARA,
                                            "apiplacas_ttl_dias": "90"})
        assert r.status_code == 200, r.text
        cfg = config.carregar()
        assert cfg["apiplacas_token"] == "SEGREDO-PAGO"
        assert cfg["apiplacas_ttl_dias"] == "90", "o resto da tela tem de salvar normalmente"

    def test_enviar_vazio_de_proposito_agora_limpa_o_token(self, ambiente, admin):
        """A outra metade do achado A7: antes desta mudança era IMPOSSÍVEL limpar um dos
        4 campos de `CHAVES_SENSIVEIS` pela tela — todo vazio virava "preservar". Agora
        presente-e-vazio é a forma explícita de limpar, igual a `rtsp_url_custom`
        em /api/cameras."""
        config.salvar({**config.carregar(), "apiplacas_token": "SEGREDO-PAGO"})
        r = admin.post("/api/config", json={"apiplacas_token": ""})
        assert r.status_code == 200, r.text
        assert config.carregar()["apiplacas_token"] == ""

    def test_token_novo_substitui(self, ambiente, admin):
        config.salvar({**config.carregar(), "apiplacas_token": "ANTIGO"})
        admin.post("/api/config", json={"apiplacas_token": "NOVO"})
        assert config.carregar()["apiplacas_token"] == "NOVO"

    def test_auditoria_registra_a_troca_sem_gravar_o_valor(self, ambiente, admin):
        """A auditoria é permanente e legível no painel. Registrar QUE o token mudou é o
        que se quer; registrar o valor transformaria a trilha de auditoria num segundo
        lugar de onde a credencial paga vaza."""
        from app.core import banco
        admin.post("/api/config", json={"apiplacas_token": "SEGREDO-PAGO"})
        registros = banco.auditoria_listar(limit=20)
        salvou = [r for r in registros if r["acao"] == "config_salva"]
        assert salvou, "a troca de configuração tem de ficar auditada"
        assert "apiplacas_token" in salvou[0]["detalhe"], "o NOME da chave deve aparecer"
        assert "SEGREDO-PAGO" not in str(registros), "o VALOR não pode aparecer"

    def test_trocar_o_token_libera_o_disjuntor(self, ambiente, admin):
        """Sem isto, corrigir um token errado só surtiria efeito 15 min depois — e o
        operador concluiria, com razão, que a tela não funciona."""
        from app.integracoes import apiplacas
        apiplacas._pausar(9999, "token inválido (HTTP 402)")
        assert apiplacas._pausado() != ""
        admin.post("/api/config", json={"apiplacas_token": "TOKEN-CORRIGIDO"})
        assert apiplacas._pausado() == "", "a pausa devia ter sido liberada ao salvar"

    def test_salvar_outra_chave_nao_libera_o_disjuntor(self, ambiente, admin):
        """A pausa existe para não insistir no que não pode dar certo. Só a troca do
        token é motivo para reabrir — senão qualquer save da tela viraria um retry."""
        from app.integracoes import apiplacas
        apiplacas.limpar_pausa()
        apiplacas._pausar(9999, "limite de consultas atingido (HTTP 429)")
        admin.post("/api/config", json={"apiplacas_ttl_dias": "90"})
        assert apiplacas._pausado() != ""
        apiplacas.limpar_pausa()


class TestTelaSabeDizerSeEstaDePe:
    def test_uso_informa_se_ha_token(self, ambiente, admin):
        """Endpoint mantido mesmo depois do achado A7 (que fez `GET /api/config` passar
        a distinguir configurado/vazio via `redacao.MASCARA`): `token_configurado`
        continua sendo o sinal mais direto para a tela, sem depender de comparar o
        valor mascarado contra a constante da máscara."""
        assert admin.get("/api/apiplacas/uso").json()["token_configurado"] is False
        config.salvar({**config.carregar(), "apiplacas_token": "SEGREDO-PAGO"})
        u = admin.get("/api/apiplacas/uso").json()
        assert u["token_configurado"] is True
        assert "SEGREDO-PAGO" not in str(u), "o indicador não pode vazar o valor"

    def test_uso_informa_se_esta_ligado(self, ambiente, admin):
        config.salvar({**config.carregar(), "apiplacas_ativo": "sim"})
        assert admin.get("/api/apiplacas/uso").json()["ativo"] is True

    def test_uso_e_so_para_admin(self, ambiente, cliente_logado):
        assert cliente_logado.get("/api/apiplacas/uso").status_code == 403


class TestFormulario:
    """A tela é montada por convenção (`name=` casa com a chave de `PADROES`), então o que
    a protege são checagens estruturais como estas."""

    def test_todos_os_campos_estao_dentro_do_form(self):
        """`salvar()` faz `new FormData(form)`: um campo fora do <form> é aceito na tela,
        não dá erro nenhum, e simplesmente nunca é gravado."""
        inicio = HTML.index("<form id=\"form-config\"")
        fim = HTML.index("</form>", inicio)
        dentro = HTML[inicio:fim]
        for campo in re.findall(r'name="(apiplacas_[a-z_]+)"', HTML):
            assert f'name="{campo}"' in dentro, f"{campo} está fora do <form>"

    def test_todo_campo_da_tela_existe_em_padroes(self):
        """Divergir faz o POST recusar a tela INTEIRA com 400, não só o campo novo."""
        for campo in set(re.findall(r'name="(apiplacas_[a-z_]+)"', HTML)):
            assert campo in config.PADROES, f"{campo} não existe em config.PADROES"

    def test_toda_chave_apiplacas_e_editavel_na_tela(self):
        """O contrário do teste acima: chave que existe mas não aparece na tela só pode
        ser mudada editando o config.txt à mão, o que na prática significa que ninguém
        muda. `apiplacas_url` é a exceção deliberada (é ponto de teste, não operação)."""
        na_tela = set(re.findall(r'name="(apiplacas_[a-z_]+)"', HTML))
        esperadas = {k for k in config.PADROES if k.startswith("apiplacas_")} - {"apiplacas_url"}
        assert esperadas <= na_tela, f"sem campo na tela: {sorted(esperadas - na_tela)}"

    def test_token_e_campo_de_senha(self):
        """Credencial paga não pode ficar legível na tela nem ser sugerida pelo
        autocomplete do navegador."""
        campo = re.search(r'<input[^>]*name="apiplacas_token"[^>]*>', HTML).group(0)
        assert 'type="password"' in campo
        assert 'autocomplete="new-password"' in campo

    def test_botao_de_saldo_nao_tem_name(self):
        """`test_config_form.py` casa todo `name=` do HTML contra `PADROES`; um `name` no
        botão faria o POST da tela INTEIRA falhar com 400. Ele também precisa ser
        `type="button"`: dentro de um <form>, o default é `submit`."""
        botao = re.search(r'<button[^>]*verSaldoApiPlacas[^>]*>', HTML).group(0)
        assert "name=" not in botao
        assert 'type="button"' in botao
