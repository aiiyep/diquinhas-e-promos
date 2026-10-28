"""Filtros de público: o supergrupo é do nicho feminino, então produtos masculinos não são postados."""

import re
import unicodedata

# Termos que sempre indicam produto masculino (comparados sem acento e em minúsculas, palavra inteira)
_TERMOS_MASCULINOS = re.compile(
    r"\b("
    r"homem|homens|masculino|masculinos|masculina|masculinas|menino|meninos|"
    r"men|man|male|"
    r"barbeador|barbeadores|barbear|barbeado|barba|barbas|barbearia|barbeiro|"
    r"pos ?barba|after ?shaves?|shavers?|beards?"
    r")\b"
)

# Termos de aparelhos de corte que só passam quando o produto se declara feminino
_TERMOS_CORTE = re.compile(r"\b(aparador|aparadores|maquininha|maquina de cortar|cortar cabelo|cortador de cabelo)\b")
_FEMININO = re.compile(r"\b(feminino|feminina|femininos|femininas|mulher|mulheres|women|female)\b")

# Valores do atributo GENDER do catálogo do ML aceitos no grupo
_GENEROS_PERMITIDOS = {"feminino", "feminina", "mulher", "mulheres", "unissex", "unisex"}
# Valores que equivalem a "não informado" (o filtro de título decide)
_GENEROS_NEUTROS = {"sem genero", "nao se aplica"}


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[_\-/]+", " ", sem_acento.lower())


def eh_produto_masculino(*textos: str) -> bool:
    """
    True se qualquer um dos textos (título, gênero, domínio do catálogo…) indicar produto
    masculino: qualquer variante de homem/masculino/menino/barbeador/barba, ou aparelho de
    corte que não se declare feminino.
    """
    texto = _normalizar(" ".join(t for t in textos if t))
    if _TERMOS_MASCULINOS.search(texto):
        return True
    return bool(_TERMOS_CORTE.search(texto) and not _FEMININO.search(texto))


def genero_permitido(generos: list[str]) -> bool:
    """
    Atributo GENDER do catálogo: sem valor (ou "Sem gênero"), o produto passa e o filtro de
    título decide; com valor, só passa se algum deles for Feminino, Mulher ou Unissex.
    """
    valores = [_normalizar(g).strip() for g in generos if g and g.strip()]
    valores = [v for v in valores if v not in _GENEROS_NEUTROS]
    return not valores or any(v in _GENEROS_PERMITIDOS for v in valores)
