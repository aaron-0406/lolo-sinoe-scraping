"""CSS/XPath selectors for the SINOE login page.

These are CANDIDATES based on prior research of the JSF/PrimeFaces stack.
They MUST be verified empirically (see Fase B of the plan, scripts/capture_login_html.py)
before relying on them. JSF auto-generates ids with ":" separator, so we use
suffix-match (id$="...") to be resilient to form id prefix changes.
"""

# Login form fields. Confirm with capture_login_html.py before final lock.
LOGIN_USERNAME_CANDIDATES: list[str] = [
    "input[id$=':usuario']",
    "input[id$=':username']",
    "input[id$=':user']",
    "input[name='usuario']",
    "input[type='text']:not([style*='display: none'])",
]

LOGIN_PASSWORD_CANDIDATES: list[str] = [
    "input[id$=':password']",
    "input[id$=':clave']",
    "input[id$=':pwd']",
    "input[type='password']",
]

LOGIN_CAPTCHA_IMG_CANDIDATES: list[str] = [
    "img[id$=':captchaImg']",
    "img[id$=':captcha']",
    "img[src*='captcha']",
    "img[src*='Captcha']",
]

LOGIN_CAPTCHA_INPUT_CANDIDATES: list[str] = [
    "input[id$=':captchaText']",
    "input[id$=':captcha']",
    "input[name*='captcha']",
]

LOGIN_SUBMIT_CANDIDATES: list[str] = [
    "button[id$=':btnIngresar']",
    "button[id$=':btnLogin']",
    "button[type='submit']",
    "input[type='submit']",
]

LOGIN_ERROR_CANDIDATES: list[str] = [
    ".ui-messages-error-summary",
    ".ui-messages-error",
    ".error-message",
    ".alert-danger",
    "[class*='error']:not(:empty)",
]

POST_LOGIN_INDICATORS: list[str] = [
    "a[href*='logout']",
    "a[href*='cerrarSesion']",
    "a[href*='salir']",
    "[id*='bandeja']",
    "[class*='bandeja']",
]

LOGIN_URL_PATH = "/sinoe/login.xhtml"
