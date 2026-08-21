"""Camada de dados (SQLite).

Era um módulo único de ~1100 linhas cobrindo detecção, cadastro multi-tenant, log de
chamadas e acesso. Virou pacote dividido por domínio, mas a fachada é a mesma: todo o
código existente faz `from app.core import banco` e chama `banco.alguma_coisa()`, e
continua funcionando — os submódulos são detalhe interno.

  _base       conexão, transação, helpers de data/código
  _esquema    criação das tabelas e migrações
  _deteccoes  detecções, listas branca/negra, retenção
  _cadastro   câmeras e a árvore entidade → empresa → automação → bico
  _chamadas   log das chamadas do roteador
  _acesso     usuários e sessões
  _auditoria  log de auditoria e tokens de reset/convite de senha
"""
from __future__ import annotations

from ._base import (
    caminho,
    conexao,
    cursor,
    definir_caminho,
    fechar_conexao,
)
from ._esquema import inicializar
from ._deteccoes import (
    ORIGENS_FILTRO,
    atualizar_deteccao,
    contar_deteccoes_placa,
    deteccoes_e_chamadas_antigas,
    listar_deteccoes,
    listas_buscar,
    listas_inserir,
    listas_listar,
    listas_remover,
    registrar_deteccao,
    remover_deteccao,
    stats,
    ultima_deteccao_bico,
    ultima_deteccao_camera,
)
from ._cadastro import (
    automacoes_atualizar,
    automacoes_inserir,
    automacoes_listar,
    automacoes_obter,
    automacoes_obter_por_codigo,
    automacoes_remover,
    bico_limpar_roi,
    bico_salvar_roi,
    bico_verificar_ativo,
    bicos_atualizar,
    bicos_inserir,
    bicos_listar,
    bicos_obter,
    bicos_obter_por_codigo,
    bicos_remover,
    cameras_atualizar,
    cameras_do_bico,
    slots_do_bico,
    cameras_inserir,
    cameras_listar,
    cameras_obter,
    cameras_remover,
    empresas_atualizar,
    empresas_definir_retencao,
    empresas_gerar_api_key,
    empresas_inserir,
    empresas_listar,
    empresas_obter,
    empresas_obter_por_cnpj,
    empresas_remover,
    empresas_revogar_api_key,
    entidades_atualizar,
    entidades_inserir,
    entidades_listar,
    entidades_obter,
    entidades_remover,
    resolver_bico,
)
from ._chamadas import chamadas_listar, chamadas_resumo, registrar_chamada
from ._acesso import (
    buscar_usuario_email,
    buscar_usuario_id,
    contar_usuarios,
    criar_usuario,
    sessao_criar,
    sessao_remover,
    sessao_renovar,
    sessao_resolver,
    sessoes_limpar_expiradas,
    sessoes_listar_do_usuario,
    sessoes_remover_do_usuario,
    usuarios_atualizar,
    usuarios_contar_admins_ativos,
    usuarios_definir_senha,
    usuarios_listar,
    usuarios_marcar_login,
    usuarios_remover,
)
from ._auditoria import (
    auditoria_listar,
    auditoria_registrar,
    reset_token_criar,
    reset_token_marcar_usado,
    reset_token_resolver,
    reset_tokens_limpar_expirados,
)

__all__ = [n for n in dir() if not n.startswith("_")]
