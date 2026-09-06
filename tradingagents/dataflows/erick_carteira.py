"""Nome antigo deste módulo. Só existe porque um teste ainda não commitado importa
por ele; a implementação vive em ``analista_carteira`` e este arquivo deve ser
apagado quando esse teste migrar."""
import sys

from . import analista_carteira as _m

sys.modules[__name__] = _m
