import os, requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
PAGES = {"jaabz_search": "https://jaabz.com/jobs?keyword=.net&remote=1"}

os.makedirs("pages", exist_ok=True)
for name, url in PAGES.items():
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "tr,en;q=0.8"}, timeout=30)
        ext = "txt" if url.endswith(".txt") else "html"
        with open(f"pages/{name}.{ext}", "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"{r.status_code}  {len(r.text):>8} chars  "
              f"jsonld={'application/ld+json' in r.text}  next={'__NEXT_DATA__' in r.text}  {name}")
    except Exception as e:
        print(f"ERR  {name}: {e}")
print("done -> zip the 'pages' folder and upload it")