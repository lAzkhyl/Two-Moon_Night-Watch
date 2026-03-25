# =====
# MODULE: utils/emojis.py
# =====
# Architecture Overview:
# Centralises every custom emoji string and static asset URL that originates
# from the Asset Server (a private Discord guild identified by ASSET_SERVER_ID).
#
# The bot must be a member of that guild for animated emoji rendering to work.
# Thumbnail URLs are Discord CDN attachment links — they carry an expiry token
# (?ex=...) and will stop loading once that timestamp passes. Refresh them by
# re-uploading the GIFs to the asset server channel and updating the constants below.
# =====

# ---------------------------------------------------------------------------
# Animated status indicators
# Source: Asset Server — #assets channel
# ---------------------------------------------------------------------------
TICK_ACTIVE  = "<a:legit_tick:1486426820516511805>"
ALERT_PAUSED = "<a:rf_alert:1486426873016750171>"

# ---------------------------------------------------------------------------
# Embed thumbnail GIFs
# Source: Asset Server — attachment CDN URLs (refresh if expired)
# ---------------------------------------------------------------------------
THUMBNAIL_ADMIN = (
    "https://media.discordapp.net/attachments/1486433301928476682"
    "/1486433333939273960"
    "/original-b59025184ca4d86a1541624ecc463c1e-ezgif.com-optimize.gif"
    "?ex=69c57c6b&is=69c42aeb"
    "&hm=7acb7605ea5e3a5a051571d714d40e2ae8d62edfc5e50067f42cdbe0bbb5cac5&="
)

THUMBNAIL_NAV = (
    "https://media.discordapp.net/attachments/1486433301928476682"
    "/1486436771272327383"
    "/original-a3006236a9dbbfbdeaad1e29330f85d7-ezgif.com-optimize.gif"
    "?ex=69c57f9e&is=69c42e1e"
    "&hm=3bb45e4663443bbc7362adec18bf40a4b218a3bbfa4b064a44a60cedd6413864&="
)