"""CSS selectors for the SINOE login page.

Verified empirically against tests/fixtures/login_page.html on 2026-04-30.

Notes from the real DOM:
- The form is `<form id="frmLogin" action="/sinoe/login.xhtml" method="post">`.
- **Username and password inputs use obfuscated, randomized `name` attributes**
  (e.g. `name="P5qKey@kVBiG2Yn2dPEwG3&@n"`) and have **no `id`**. We rely on
  `placeholder` + `tabindex` + `type` to select them — those are stable.
- The CAPTCHA image has `id="frmLogin:imgCapcha"` — note the spelling
  ("Capcha", not "Captcha"). Stable id, so suffix-match works.
- The CAPTCHA input has `id="frmLogin:captcha"` and `placeholder="Ingrese Captcha"`.
- The submit button has `id="frmLogin:btnIngresar"` — stable.
"""

LOGIN_USERNAME_CANDIDATES: list[str] = [
    "input[placeholder='Usuario']",
    "input[tabindex='1'][type='text']",
    "form#frmLogin input[type='text']:not([type='hidden'])",
]

LOGIN_PASSWORD_CANDIDATES: list[str] = [
    "input[placeholder='Contraseña']",
    "input[type='password']",
    "input[tabindex='2'][type='password']",
]

LOGIN_CAPTCHA_IMG_CANDIDATES: list[str] = [
    "img[id$=':imgCapcha']",
    "img[title='Imagen captcha']",
    "img[id*='Capcha']",
]

LOGIN_CAPTCHA_INPUT_CANDIDATES: list[str] = [
    "input[id$=':captcha']",
    "input[placeholder='Ingrese Captcha']",
    "input[tabindex='3']",
]

LOGIN_SUBMIT_CANDIDATES: list[str] = [
    "button[id$=':btnIngresar']",
    "button[title='Ingresar']",
    "button.btn-red[type='submit']",
]

LOGIN_ERROR_CANDIDATES: list[str] = [
    ".ui-messages-error-summary",
    ".ui-messages-error",
    ".ui-message-error",
    ".alert-danger",
    "[class*='error']:not(:empty)",
]

POST_LOGIN_INDICATORS: list[str] = [
    # Verified empirically 2026-04-30: post-login lands on the "Servicios en Línea"
    # hub at /sinoe/login.xhtml (URL unchanged) with these stable elements.
    "a[id$=':clCerrarSession']",  # CERRAR SESIÓN command link
    "a[id$=':clMisDatos']",  # MIS DATOS command link
    "a[id$=':clCambioClave']",  # CAMBIO DE CLAVE command link
    "form#frmNuevo",  # post-login form
    "span.txtuser",  # user name display
    # Fallbacks
    "a[href*='logout']",
    "a[href*='cerrarSesion']",
    "[id*='bandeja']",
]

LOGIN_URL_PATH = "/sinoe/login.xhtml"
