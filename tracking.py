"""Meta Pixel snippet, shared by every page template: the Dash app's
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
// One-time CompleteRegistration, flagged by the cookie the sign-up view
// sets on its redirect (see SIGNUP_EVENT_COOKIE) -- cleared right away so
// a reload doesn't count the same sign-up twice.
if (document.cookie.split('; ').indexOf('{SIGNUP_EVENT_COOKIE}=1') !== -1) {{
  fbq('track', 'CompleteRegistration');
  document.cookie = '{SIGNUP_EVENT_COOKIE}=; Max-Age=0; path=/; SameSite=Lax';
}}
</script>
<!-- End Meta Pixel Code -->"""

META_PIXEL_BODY = (f'<noscript><img height="1" width="1" style="display:none" alt="" '
                   f'src="https://www.facebook.com/tr?id={META_PIXEL_ID}&amp;ev=PageView&amp;noscript=1">'
                   '</noscript>')
