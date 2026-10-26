import tweepy
from formatters.promo_formatter import FormattedPromotion
from publishers.base import BasePublisher


class TwitterPublisher(BasePublisher):
    """Publicador para a API v2 do Twitter/X utilizando Tweepy."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        bearer_token: str,
    ) -> None:
        self.client = tweepy.Client(
            bearer_token=bearer_token,
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )

    async def publish(self, promotion: FormattedPromotion) -> bool:
        """Publica o texto da oferta no Twitter/X."""
        # TODO: Implementar envio de tweet (e upload opcional de mídia via API v1.1)
        pass
