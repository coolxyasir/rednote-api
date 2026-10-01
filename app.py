from flask import Flask, request, jsonify, Response
from curl_cffi import requests as cr
import re
from urllib.parse import unquote, quote, urljoin

app = Flask(__name__)

UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
UA_MOB = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def home():
    return jsonify({"service": "rednote-api", "version": "curl_cffi-v3-extractfix"})


def unesc(s):
    out = s.replace("\\u002F", "/").replace("\\/", "/")
    out = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), out)
    # HTML-escaped JSON bhi handle karo (&quot; wala case)
    out = out.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    out = out.replace("&#x2F;", "/").replace("&#47;", "/")
    return out


def find_video(flat):
    m = re.search(r'"masterUrl":"(https:[^"]+)"', flat)
    if m:
        return m.group(1)
    m = re.search(r'"streamUrl":"(https:[^"]+)"', flat)
    if m:
        return m.group(1)
    m = re.search(r'"videoUrl":"(https:[^"]+\.mp4[^"]*)"', flat)
    if m:
        return m.group(1)
    m = re.search(r'(https://sns-video[^"\s\'<>]+?\.mp4[^"\s\'<>]*)', flat)
    if m:
        return m.group(1)
    # backup/other CDN pattern
    m = re.search(r'(https://[^"\s\'<>]+?\.mp4\?[^"\s\'<>]+)', flat)
    if m:
        return m.group(1)
    return None


@app.route("/api/rednote")
def rednote():
    link = (request.args.get("url") or "").strip()
    dl = request.args.get("dl") == "1"
    debug = request.args.get("debug") == "1"

    if not link:
        return jsonify({"error": "Link missing hai"}), 400
    if not link.startswith("http"):
        link = "https://" + link
    if not re.search(r"(xiaohongshu|xhslink)\.com", link, re.I):
        return jsonify({"error": "Ye RedNote link nahi hai"}), 400

    try:
        s = cr.Session(impersonate="chrome124")

        # 1) Short link resolve
        final = link
        for _ in range(5):
            r = s.get(final, allow_redirects=False, headers={
                "User-Agent": UA_MOB,
                "Referer": "https://www.xiaohongshu.com/"
            }, timeout=15)
            loc = r.headers.get("Location")
            if loc:
                final = urljoin(final, loc)
                continue
            break

        # 2) Login/captcha redirect -> redirectPath
        for _ in range(2):
            if re.search(r"xiaohongshu\.com/(login|website-login)", final, re.I):
                m = re.search(r"redirectPath=([^&]+)", final)
                if m:
                    rp = unquote(m.group(1))
                    if "%3A%2F%2F" in rp or "%3F" in rp:
                        rp = unquote(rp)
                    final = rp

        # 3) Note ID + token
        m = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)", final)
        if not m:
            return jsonify({"error": "Note ID nahi mili", **({"finalUrl": final} if debug else {})}), 400
        note_id = m.group(1)

        t = re.search(r"xsec_token=([^&]+)", final)
        token = t.group(1) if t else ""
        src = re.search(r"xsec_source=([^&]+)", final)
        source = src.group(1) if src else "app_share"

        # 4) Do page URLs try karo (explore + discovery) — dono alag SSR dete hain
        urls_to_try = [
            ("explore-pc", f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={quote(token)}&xsec_source={quote(source)}", UA_PC),
            ("discovery-mobile", f"https://www.xiaohongshu.com/discovery/item/{note_id}?xsec_token={quote(token)}&xsec_source={quote(source)}", UA_MOB),
        ]

        video = None
        cover = None
        title = ""
        desc = ""
        images = []
        best_flat = ""
        best_html = ""

        for name, web, ua in urls_to_try:
            try:
                page = s.get(web, headers={
                    "User-Agent": ua,
                    "Referer": "https://www.xiaohongshu.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
                }, timeout=20)
                html = page.text
                flat = unesc(html)

                v = find_video(flat)
                if v:
                    video = v
                    cm = re.search(r'"urlPre":"(https:[^"]+)"', flat) or \
                         re.search(r'"urlDefault":"(https:[^"]+)"', flat) or \
                         re.search(r'og:image[^>]+content="([^"]+)"', flat)
                    cover = cm.group(1) if cm else None
                    tm = re.search(r'"title":"([^"]*)"', flat) or re.search(r"<title>([^<]+)</title>", flat)
                    title = tm.group(1) if tm else ""
                    dm = re.search(r'"desc":"([^"]*)"', flat)
                    desc = dm.group(1) if dm else ""
                    break

                # partial data yaad rakho (agar dono fail hue to debug me kaam aayega)
                if "noteDetailMap" in flat and not best_flat:
                    best_flat = flat
                    best_html = html
            except Exception:
                continue

        if debug and not video:
            # noteDetailMap ke aas-paas ka content dikhao
            idx = best_flat.find("noteDetailMap")
            snippet = best_flat[idx:idx+900] if idx >= 0 else "(noteDetailMap nahi mila)"
            return jsonify({
                "error": "Video URL nahi mila", "noteId": note_id,
                "stateFound": "__INITIAL_STATE__" in best_html,
                "hasMasterUrl": "masterUrl" in best_flat,
                "hasStreamUrl": "streamUrl" in best_flat,
                "hasMp4": ".mp4" in best_flat,
                "noteNull": ('"note":null' in best_flat) or ('"note": null' in best_flat),
                "pageTitle": (re.search(r"<title>([^<]+)</title>", best_html).group(1) if re.search(r"<title>([^<]+)</title>", best_html) else None),
                "stateSnippet": snippet[:800]
            }), 200

        # images nikalo (dono flat se — video milne ke case me bhi cover ke liye)
        if not video:
            seen = set()
            for im in re.finditer(r'"urlDefault":"(https:[^"]+)"', best_flat or ""):
                if im.group(1) not in seen:
                    seen.add(im.group(1))
                    images.append(im.group(1))
            images = images[:10]

        # Image post
        if not video and images:
            return jsonify({
                "platform": "rednote", "type": "images",
                "title": (title or desc or "RedNote Post").strip(),
                "cover": cover or images[0], "images": images
            })

        if not video:
            return jsonify({"error": "Video URL nahi mila - debug=1 ke saath try karo"}), 200

        # 5) Direct download
        if dl:
            rr = s.get(video, headers={"User-Agent": UA_PC, "Referer": "https://www.xiaohongshu.com/"},
                       stream=True, timeout=30)

            def gen():
                for chunk in rr.iter_content(65536):
                    if chunk:
                        yield chunk

            return Response(gen(), headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": f'attachment; filename="rednote-{note_id}.mp4"'
            })

        return jsonify({
            "platform": "rednote", "type": "video",
            "title": (title or desc or "RedNote Video").strip(),
            "cover": cover, "videoUrl": video,
            "download": request.url_root.rstrip("/") + "/api/rednote?url=" + quote(link) + "&dl=1"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
