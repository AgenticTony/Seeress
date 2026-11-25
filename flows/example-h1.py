# seeress-flow: example-h1 — robust version: retry once if the page errors
new_tab("https://www.iana.org/help/example-domains")
wait_for_load()
title = js("document.title")
if "Example Domains" not in str(title):
    goto_url("https://www.iana.org/help/example-domains")
    wait_for_load()
h1_text = js("document.querySelector('h1').innerText")
print(f"summary: h1 = {h1_text}")
