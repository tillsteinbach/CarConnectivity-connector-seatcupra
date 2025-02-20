"""Implements a custom Retry class that allows for blacklisting certain status codes that will not retry."""
from urllib3.util.retry import Retry


class BlacklistRetry(Retry):
    """
    BlacklistRetry class extends the Retry class to include a blacklist of status codes that should not be retried.
    """
    def __init__(self, status_blacklist=None, **kwargs) -> None:
        self.status_blacklist = status_blacklist
        super().__init__(**kwargs)

    def is_retry(self, method, status_code, has_retry_after=False) -> bool:
        """
        Determines if a request should be retried based on the HTTP method, status code,
        and the presence of a 'Retry-After' header.

        Args:
            method (str): The HTTP method of the request (e.g., 'GET', 'POST').
            status_code (int): The HTTP status code of the response.
            has_retry_after (bool): Indicates if the response contains a 'Retry-After' header.

        Returns:
            bool: True if the request should be retried, False otherwise.
        """
        if self.status_blacklist is not None and status_code in self.status_blacklist:
            return False
        else:
            return super().is_retry(method, status_code, has_retry_after)
