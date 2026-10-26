from abc import ABC, abstractmethod
from formatters.promo_formatter import FormattedPromotion


class BasePublisher(ABC):
    """Interface contrato para disparo de promoções em canais de destino."""

    @abstractmethod
    async def publish(self, promotion: FormattedPromotion) -> bool:
        """Publica uma oferta formatada no canal de destino."""
        pass
