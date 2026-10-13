"""The default values of the client (clients/spec/README.md, sections 1 and 3)."""

#: The base URL when neither the option `base_url` nor `OSCAR_BASE_URL` is set: the production origin.
#: The API is under `/api/` and OAuth under `/oauth/`.
DEFAULT_BASE_URL = 'https://oscaraccountant.com'

#: The timeout of one attempt, in seconds.
DEFAULT_TIMEOUT = 60.0

#: The retries after the first attempt. 0 turns them off.
DEFAULT_MAX_RETRIES = 2

ENV_API_KEY = 'OSCAR_API_KEY'
ENV_BASE_URL = 'OSCAR_BASE_URL'
ENV_PROFILE = 'OSCAR_PROFILE'
ENV_USE_CLI_LOGIN = 'OSCAR_USE_CLI_LOGIN'
