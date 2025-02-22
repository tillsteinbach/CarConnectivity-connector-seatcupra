
"""
This module provides utility functions and classes for handling authentication and parsing HTML forms
and scripts for the seatcupra car connectivity connector.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import re
from html.parser import HTMLParser

if TYPE_CHECKING:
    from typing import Optional, Dict


def add_bearer_auth_header(token, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Adds a Bearer token to the Authorization header.

    Args:
        token (str): The Bearer token to be added to the headers.
        headers (Optional[Dict[str, str]]): An optional dictionary of headers to which the Authorization header will be added.
                                            If not provided, a new dictionary will be created.

    Returns:
        Dict[str, str]: The headers dictionary with the added Authorization header.
    """
    headers = headers or {}
    headers['Authorization'] = f'Bearer {token}'
    return headers


class HTMLFormParser(HTMLParser):
    """
    A custom HTML parser to extract form data from HTML content.
    """
    def __init__(self, form_id) -> None:
        super().__init__()
        self._form_id = form_id
        self._inside_form: bool = False
        self.target = None
        self.data = {}

    def _get_attr(self, attrs, name):
        for attr in attrs:
            if attr[0] == name:
                return attr[1]
        return None

    def handle_starttag(self, tag, attrs) -> None:
        if self._inside_form and tag == 'input':
            self.handle_input(attrs)
            return

        if tag == 'form' and self._get_attr(attrs, 'id') == self._form_id:
            self._inside_form = True
            self.target = self._get_attr(attrs, 'action')

    def handle_endtag(self, tag) -> None:
        if tag == 'form' and self._inside_form:
            self._inside_form = False

    def handle_input(self, attrs) -> None:
        if not self._inside_form:
            return

        name = self._get_attr(attrs, 'name')
        value = self._get_attr(attrs, 'value')

        if name:
            self.data[name] = value


class ScriptFormParser(HTMLParser):
    fields: list[str] = []
    targetField: str = ''

    def __init__(self):
        super().__init__()
        self._inside_script = False
        self.data = {}
        self.target = None

    def handle_starttag(self, tag, attrs) -> None:
        if not self._inside_script and tag == 'script':
            self._inside_script = True

    def handle_endtag(self, tag) -> None:
        if self._inside_script and tag == 'script':
            self._inside_script = False

    def handle_data(self, data) -> None:
        if not self._inside_script:
            return

        match: re.Match[str] | None = re.search(r'templateModel: (.*?),\n', data)
        if not match:
            return

        result = json.loads(match.group(1))
        self.target = result.get(self.targetField, None)
        self.data = {k: v for k, v in result.items() if k in self.fields}

        match2 = re.search(r'csrf_token: \'(.*?)\'', data)
        if match2:
            self.data['_csrf'] = match2.group(1)


class CredentialsFormParser(ScriptFormParser):
    fields: list[str] = ['relayState', 'hmac', 'registerCredentialsPath', 'error', 'errorCode']
    targetField: str = 'postAction'


class TermsAndConditionsFormParser(ScriptFormParser):
    fields: list[str] = ['relayState', 'hmac', 'countryOfResidence', 'legalDocuments']
    targetField: str = 'loginUrl'

    def handle_data(self, data) -> None:
        if not self._inside_script:
            return

        super().handle_data(data)

        if 'countryOfResidence' in self.data:
            self.data['countryOfResidence'] = self.data['countryOfResidence'].upper()

        if 'legalDocuments' not in self.data:
            return

        for key in self.data['legalDocuments'][0]:
            # Skip unnecessary keys
            if key in ('skipLink', 'declineLink', 'majorVersion', 'minorVersion', 'changeSummary'):
                continue

            # Move values under a new key while converting boolean values to 'yes' or 'no'
            v = self.data['legalDocuments'][0][key]
            self.data[f'legalDocuments[0].{key}'] = ('yes' if v else 'no') if isinstance(v, bool) else v

        # Remove the original object
        del self.data['legalDocuments']
