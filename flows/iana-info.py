# seeress-flow: iana-info — example flow: open the IANA example-domains page, print title + h1
new_tab("https://www.iana.org/help/example-domains")
wait_for_load()
title = js("document.title")
h1 = js("document.querySelector('h1') ? document.querySelector('h1').innerText : '(none)'")
print(f"summary: title={title} h1={h1}")
