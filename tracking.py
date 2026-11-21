"""Meta Pixel and TikTok Pixel snippets, shared by every page template: the Dash app's
index_string (dash_app.py), the account pages (accounts.py) and the
crawlable manager/politician pages (seo_pages.py).

Split from Meta's single copy-paste block into a <head> script and a
<body> <noscript> fallback -- an <img> isn't valid inside <head>.

Left off the password-reset pages (accounts._reset_page and
_invalid_reset_link_page): the pixel reports each page's URL and can read
form fields, and those pages are about a specific account's credentials.

The Dash app is a single page, so PageView fires once per page load, not
per tracker tab switch.
"""

META_PIXEL_ID = "1633010104933806"
TIKTOK_PIXEL_ID = "DC10KG3C77U8S1HK4UTG"
# Set (readable by JS, so not HttpOnly) by accounts.py's sign-up view on its
# redirect after a new account is created; the pixel snippet below fires
# CompleteRegistration when it sees it and deletes it. A cookie rather than
# a "?welcome=1" URL so a shared or reloaded link can't fire fake sign-ups.
SIGNUP_EVENT_COOKIE = "sp_new_signup"

META_PIXEL_HEAD = f"""<!-- Meta Pixel Code -->
<script>
!function(f,b,e,v,n,t,s)
{{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];
s.parentNode.insertBefore(t,s)}}(window, document,'script',
'https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{META_PIXEL_ID}');
fbq('track', 'PageView');
</script>
<!-- End Meta Pixel Code -->"""

_TIKTOK_PIXEL = f"""<!-- TikTok Pixel Code Start -->
<script>
!function (w, d, t) {{
  w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[];ttq.methods=["page","track","identify","instances","debug","on","off","once","ready","alias","group","enableCookie","disableCookie","holdConsent","revokeConsent","grantConsent"],ttq.setAndDefer=function(t,e){{t[e]=function(){{t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}}};for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);ttq.instance=function(t){{for(
var e=ttq._i[t]||[],n=0;n<ttq.methods.length;n++)ttq.setAndDefer(e,ttq.methods[n]);return e}},ttq.load=function(e,n){{var r="https://analytics.tiktok.com/i18n/pixel/events.js",o=n&&n.partner;ttq._i=ttq._i||{{}},ttq._i[e]=[],ttq._i[e]._u=r,ttq._t=ttq._t||{{}},ttq._t[e]=+new Date,ttq._o=ttq._o||{{}},ttq._o[e]=n||{{}};n=document.createElement("script")
;n.type="text/javascript",n.async=!0,n.src=r+"?sdkid="+e+"&lib="+t;e=document.getElementsByTagName("script")[0];e.parentNode.insertBefore(n,e)}};

  ttq.load('{TIKTOK_PIXEL_ID}');
  ttq.page();
}}(window, document, 'ttq');
</script>
<!-- TikTok Pixel Code End -->"""

# One-time CompleteRegistration on both pixels, flagged by the cookie the
# sign-up view sets on its redirect (see SIGNUP_EVENT_COOKIE) -- cleared
# right away so a reload doesn't count the same sign-up twice.
_SIGNUP_EVENT = f"""<script>
if (document.cookie.split('; ').indexOf('{SIGNUP_EVENT_COOKIE}=1') !== -1) {{
  fbq('track', 'CompleteRegistration');
  ttq.track('CompleteRegistration');
  document.cookie = '{SIGNUP_EVENT_COOKIE}=; Max-Age=0; path=/; SameSite=Lax';
}}
</script>"""

# Everything that goes in <head>: both pixels, then the sign-up event.
PIXEL_HEAD = "\n".join([META_PIXEL_HEAD, _TIKTOK_PIXEL, _SIGNUP_EVENT])

META_PIXEL_BODY = (f'<noscript><img height="1" width="1" style="display:none" alt="" '
                   f'src="https://www.facebook.com/tr?id={META_PIXEL_ID}&amp;ev=PageView&amp;noscript=1">'
                   '</noscript>')
