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
    return jsonify({"service": "rednote-api", "version": "curl_cffi-v6-dual-ks-rn"})


def unesc(s):
    out = s.replace("\\u002F", "/").replace("\\/", "/")
    out = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), out)
    out = out.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    out = out.replace("&#x2F;", "/").replace("&#47;", "/")
    return out


def full_unquote(s):
    for _ in range(4):
        if "%" not in s:
            break
        try:
            new = unquote(s)
            if new == s:
                break
            s = new
        except Exception:
            break
    return s


# ══════════════════════════════════════════════
#                    REDNOTE
# ══════════════════════════════════════════════

def find_video(flat):
    for pat in [
        r'"masterUrl":"(https:[^"]+)"',
        r'"streamUrl":"(https:[^"]+)"',
        r'"videoUrl":"(https:[^"]+\.mp4[^"]*)"',
        r'(https://sns-video[^"\s\'<>]+?\.mp4[^"\s\'<>]*)',
        r'(https://[^"\s\'<>]+?\.mp4\?[^"\s\'<>]+)',
    ]:
        m = re.search(pat, flat)
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

        try:
            s.get("https://www.xiaohongshu.com/", headers={"User-Agent": UA_PC}, timeout=10)
        except Exception:
            pass

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

        original_note_url = None
        for _ in range(2):
            if re.search(r"xiaohongshu\.com/(login|website-login)", final, re.I):
                m = re.search(r"redirectPath=([^&]+)", final)
                if m:
                    rp = unquote(m.group(1))
                    original_note_url = rp
                    final = rp

        m = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)", final)
        if not m:
            return jsonify({"error": "Note ID nahi mili", **({"finalUrl": final} if debug else {})}), 400
        note_id = m.group(1)

        t = re.search(r"xsec_token=([^&]+)", final)
        token = full_unquote(t.group(1)) if t else ""
        src = re.search(r"xsec_source=([^&]+)", final)
        source = full_unquote(src.group(1)) if src else "app_share"

        candidates = []
        if original_note_url and "/discovery/item/" in original_note_url:
            candidates.append(("original-discovery", original_note_url, UA_MOB))
        candidates.append(("rebuilt-explore",
                           f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={quote(token, safe='')}&xsec_source={quote(source, safe='')}",
                           UA_PC))
        candidates.append(("rebuilt-discovery",
                           f"https://www.xiaohongshu.com/discovery/item/{note_id}?xsec_token={quote(token, safe='')}&xsec_source={quote(source, safe='')}",
                           UA_MOB))

        video = None
        cover = None
        title = ""
        desc = ""
        images = []
        best_flat = ""
        best_html = ""
        used = None

        for name, web, ua in candidates:
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
                    used = name
                    cm = re.search(r'"urlPre":"(https:[^"]+)"', flat) or \
                         re.search(r'"urlDefault":"(https:[^"]+)"', flat) or \
                         re.search(r'og:image[^>]+content="([^"]+)"', flat)
                    cover = cm.group(1) if cm else None
                    tm = re.search(r'"title":"([^"]*)"', flat) or re.search(r"<title>([^<]+)</title>", flat)
                    title = tm.group(1) if tm else ""
                    dm = re.search(r'"desc":"([^"]*)"', flat)
                    desc = dm.group(1) if dm else ""
                    break

                if "noteDetailMap" in flat and not best_flat:
                    best_flat = flat
                    best_html = html
            except Exception:
                continue

        if debug and not video:
            idx = best_flat.find("serverRequestInfo")
            errsnippet = best_flat[idx:idx + 200] if idx >= 0 else "(serverRequestInfo nahi mila)"
            return jsonify({
                "error": "Video URL nahi mila", "noteId": note_id,
                "tokenLen": len(token), "tokenTail": token[-6:] if token else None,
                "source": source,
                "hasMasterUrl": "masterUrl" in best_flat,
                "hasMp4": ".mp4" in best_flat,
                "errSnippet": errsnippet
            }), 200

        if not video:
            seen = set()
            for im in re.finditer(r'"urlDefault":"(https:[^"]+)"', best_flat or ""):
                if im.group(1) not in seen:
                    seen.add(im.group(1))
                    images.append(im.group(1))
            images = images[:10]

        if not video and images:
            return jsonify({
                "platform": "rednote", "type": "images",
                "title": (title or desc or "RedNote Post").strip(),
                "cover": cover or images[0], "images": images
            })

        if not video:
            return jsonify({"error": "Video URL nahi mila - debug=1 ke saath try karo"}), 200

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
            "platform": "rednote", "type": "video", "attempt": used,
            "title": (title or desc or "RedNote Video").strip(),
            "cover": cover, "videoUrl": video,
            "download": request.url_root.rstrip("/") + "/api/rednote?url=" + quote(link) + "&dl=1"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════
#                  KUAISHOU (NEW)
# ══════════════════════════════════════════════

def ks_extract_from_flat(flat):
    m = re.search(r'"photoUrl"\s*:\s*"([^"]+)"', flat)
    if m:
        return m.group(1)
    m = re.search(r'(https?://[^"\s\'<>]+?\.mp4[^"\s\'<>]*)', flat)
    if m:
        return m.group(1)
    return None


@app.route("/api/kuaishou")
def kuaishou():
    link = (request.args.get("url") or "").strip()
    dl = request.args.get("dl") == "1"
    debug = request.args.get("debug") == "1"

    if not link:
        return jsonify({"error": "Link missing hai"}), 400
    if not link.startswith("http"):
        link = "https://" + link
    if not re.search(r"(kuaishou|chenzhongtech)\.com", link, re.I):
        return jsonify({"error": "Ye Kuaishou link nahi hai"}), 400

    try:
        # Chrome TLS fingerprint session — Kuaishou ko bot nahi lagta
        s = cr.Session(impersonate="chrome124")

        # 1) Warm-up — homepage se did cookie lo (session khud yaad rakhta hai)
        try:
            s.get("https://www.kuaishou.com/", headers={"User-Agent": UA_PC}, timeout=10)
        except Exception:
            pass

        # 2) Short link resolve
        final = link
        for _ in range(5):
            r = s.get(final, allow_redirects=False, headers={
                "User-Agent": UA_PC,
                "Referer": "https://www.kuaishou.com/"
            }, timeout=12)
            loc = r.headers.get("Location")
            if loc:
                final = urljoin(final, loc)
                continue
            break

        # 3) Photo ID
        m = re.search(r"/short-video/([0-9A-Za-z_-]+)", final) or \
            re.search(r"/photo/([0-9A-Za-z_-]{10,})", final)
        if not m:
            return jsonify({"error": "Video ID nahi mili — sirf single video ka link do",
                            **({"finalUrl": final} if debug else {})}), 400
        photo_id = m.group(1)

        # 4) GraphQL API — session cookies (did) ke saath
        video = None
        caption = None
        cover = None
        gq_err = None
        try:
            gq = s.post("https://www.kuaishou.com/graphql",
                        headers={
                            "User-Agent": UA_PC,
                            "Content-Type": "application/json",
                            "Accept": "*/*",
                            "Referer": f"https://www.kuaishou.com/short-video/{photo_id}",
                            "Origin": "https://www.kuaishou.com"
                        },
                        json={
                            "operationName": "visionVideoDetail",
                            "variables": {"photoId": photo_id, "page": "vision"},
                            "query": "query visionVideoDetail($photoId: String, $page: String) { visionVideoDetail(photoId: $photoId, page: $page) { status photo { id caption photoUrl coverUrl } } }"
                        }, timeout=15)
            gj = gq.json()
            ph = gj.get("data", {}).get("visionVideoDetail", {}).get("photo") if isinstance(gj, dict) else None
            if ph and ph.get("photoUrl"):
                video = ph["photoUrl"]
                caption = ph.get("caption")
                cover = ph.get("coverUrl")
            elif debug:
                gq_err = str(gj)[:300]
        except Exception as e:
            gq_err = f"{type(e).__name__}: {e}"

        # 5) Fallback — HTML page se
        if not video:
            try:
                page = s.get(f"https://www.kuaishou.com/short-video/{photo_id}", headers={
                    "User-Agent": UA_PC,
                    "Referer": "https://www.kuaishou.com/"
                }, timeout=15)
                flat = unesc(page.text)
                video = ks_extract_from_flat(flat)
                if not cover:
                    cm = re.search(r'"coverUrl"\s*:\s*"([^"]+)"', flat) or \
                         re.search(r'og:image[^>]+content="([^"]+)"', flat)
                    cover = cm.group(1) if cm else None
                if not caption:
                    tm = re.search(r'"caption"\s*:\s*"([^"]*)"', flat) or \
                         re.search(r'og:title[^>]+content="([^"]+)"', flat)
                    caption = tm.group(1) if tm else None
            except Exception:
                pass

        if debug and not video:
            return jsonify({
                "error": "Video URL nahi mila", "photoId": photo_id,
                "finalUrl": final, "gqErr": gq_err
            }), 200

        if not video:
            return jsonify({"error": "Video URL nahi mila — debug=1 ke saath try karo"}), 200

        # 6) Direct download
        if dl:
            rr = s.get(video, headers={"User-Agent": UA_PC, "Referer": "https://www.kuaishou.com/"},
                       stream=True, timeout=30)

            def gen():
                for chunk in rr.iter_content(65536):
                    if chunk:
                        yield chunk

            return Response(gen(), headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": f'attachment; filename="kuaishou-{photo_id}.mp4"'
            })

        return jsonify({
            "platform": "kuaishou", "type": "video",
            "title": (caption or "Kuaishou Video").strip(),
            "cover": cover, "videoUrl": video,
            "download": request.url_root.rstrip("/") + "/api/kuaishou?url=" + quote(link) + "&dl=1"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
