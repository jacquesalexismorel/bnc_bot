import asyncio
import base64
import io
import logging
import math
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from PIL import ImageOps
from reportlab.pdfgen import canvas
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bnc-bot")

# ---------- Configuration (variables d'environnement) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
MAIL_TO = os.environ.get("MAIL_TO", "jacktrech@gmail.com")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
MODELE_PDF = Path(__file__).parent / "modele_bnc.pdf"
MAX_PHOTOS_PDF = 3

PHOTOS, CHOIX, EAN_TEXTE, EAN_PHOTO, EAN_VALID, QTE, DLC, COMMENT, DATE = range(9)


# ---------- Utilitaires EAN ----------
def ean_valide(ean: str) -> bool:
    if not re.fullmatch(r"\d{13}", ean):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(ean[:12]))
    return (10 - total % 10) % 10 == int(ean[12])


def lire_ean_zxing(image_bytes: bytes) -> str | None:
    """Lecture gratuite du code-barres, directement sur le serveur."""
    import zxing_cpp

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    for taille in (2000, 1200, 800):
        copie = img.copy()
        copie.thumbnail((taille, taille))
        for r in zxing_cpp.read_barcodes(copie):
            texte = re.sub(r"\D", "", r.text or "")
            if len(texte) == 12:  # UPC-A -> EAN-13
                texte = "0" + texte
            if ean_valide(texte):
                return texte
    return None


def lire_ean_claude(image_bytes: bytes) -> str | None:
    """Plan B (payant, facultatif) : seulement si ANTHROPIC_API_KEY est défini."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()

    msg = anthropic.Anthropic().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text",
                 "text": "Lis le code-barres EAN-13 (les 13 chiffres sous les barres). "
                         "Réponds UNIQUEMENT par les 13 chiffres, ou INCONNU."},
            ],
        }],
    )
    texte = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\d{13}", re.sub(r"\D", "", texte))
    return m.group() if m and ean_valide(m.group()) else None


def lire_ean_sync(image_bytes: bytes) -> str | None:
    try:
        ean = lire_ean_zxing(image_bytes)
        if ean:
            return ean
    except Exception:
        log.exception("Erreur zxing")
    return lire_ean_claude(image_bytes)


# ---------- Génération du PDF (sur le modèle officiel modele_bnc.pdf) ----------
BLEU = (0.14902, 0.215686, 0.27451)  # même bleu que les titres du modèle
GRIS_CASE = (0.956863, 0.960784, 0.964706)
GRIS_BORD = (0.682353, 0.713725, 0.745098)

# Cases du modèle (x, y bas, largeur, hauteur) en points PDF
CASE_QTE = (51.02, 631.80, 232.44, 65.20)
CASE_DLC = (297.64, 631.80, 232.44, 65.20)
CASE_COM = (297.64, 560.93, 232.44, 65.20)
PHOTOS_RANG1 = [(51.02, 249.29), (215.43, 249.29), (379.84, 249.29)]
PHOTO_W, PHOTO_H = 155.91, 260.79


def propre(texte: str) -> str:
    """Garde seulement les caractères que la police du PDF sait écrire."""
    return texte.encode("cp1252", errors="replace").decode("cp1252")


def pdf_str(texte: str) -> bytes:
    t = propre(texte).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return t.encode("cp1252")


def preparer_photo(data: bytes) -> ImageReader:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1400, 1400))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return ImageReader(buf)


def dessiner_photo(c, data: bytes, x: float, y: float, w: float, h: float):
    c.setFillColorRGB(*GRIS_CASE)
    c.rect(x, y, w, h, fill=1, stroke=0)
    c.drawImage(
        preparer_photo(data), x + 4, y + 4, width=w - 8, height=h - 8,
        preserveAspectRatio=True, anchor="c",
    )
    c.setStrokeColorRGB(*GRIS_BORD)
    c.setLineWidth(1)
    c.rect(x, y, w, h, fill=0, stroke=1)


def ecrire_valeur(c, case, texte: str):
    x, y, w, h = case
    c.setFillColorRGB(*BLEU)
    taille = 18
    texte = propre(texte)
    while stringWidth(texte, "Helvetica-Bold", taille) > w - 16 and taille > 8:
        taille -= 1
    c.setFont("Helvetica-Bold", taille)
    c.drawString(x + 8, y + 16, texte)


def ecrire_commentaire(c, case, texte: str):
    x, y, w, h = case
    texte = propre(texte).strip()
    if not texte:
        return
    largeur = w - 16
    haut_dispo = h - 31  # on garde la place du titre
    c.setFillColorRGB(*BLEU)
    for taille in (11, 10, 9, 8, 7):
        interligne = taille * 1.2
        lignes = simpleSplit(texte, "Helvetica", taille, largeur)
        if len(lignes) * interligne <= haut_dispo:
            break
    else:
        max_l = int(haut_dispo // interligne)
        lignes = lignes[:max_l]
        lignes[-1] = lignes[-1].rstrip()[:-1] + "…" if lignes[-1] else "…"
    c.setFont("Helvetica", taille)
    ycur = y + h - 31 - taille
    for ligne in lignes:
        c.drawString(x + 8, ycur, ligne)
        ycur -= interligne


def generer_pdf(photos: list[bytes], ean: str, quantite: int, dlc: str,
                commentaire: str, date_str: str) -> bytes:
    photos = photos[:MAX_PHOTOS_PDF]
    nb = len(photos)

    # 1) On modifie le texte du modèle : date, EAN, et on retire les "PHOTO n" inutilisés
    writer = PdfWriter(clone_from=str(MODELE_PDF))
    page = writer.pages[0]
    flux = page.get_contents().get_data()
    flux = re.sub(rb"le \d{2}/\d{2}/\d{4}\)", b"le " + date_str.encode() + b")", flux, count=1)
    flux = flux.replace(b"(EAN : 13 chiffres)", b"(EAN : " + pdf_str(ean) + b")", 1)
    for n in range(nb + 1, 4):
        flux = flux.replace(b"(PHOTO %d)" % n, b"()", 1)
    nouveau = DecodedStreamObject()
    nouveau.set_data(flux)
    page[NameObject("/Contents")] = writer._add_object(nouveau)

    # 2) On crée une couche avec les valeurs et les photos, posée par-dessus
    couche = io.BytesIO()
    c = canvas.Canvas(couche, pagesize=A4)
    ecrire_valeur(c, CASE_QTE, str(quantite))
    ecrire_valeur(c, CASE_DLC, dlc)
    ecrire_commentaire(c, CASE_COM, commentaire)
    for i, (x, y) in enumerate(PHOTOS_RANG1):
        if i < nb:
            dessiner_photo(c, photos[i], x, y, PHOTO_W, PHOTO_H)
        else:  # cadre vide : on le masque complètement
            c.setFillColorRGB(1, 1, 1)
            c.rect(x - 2, y - 2, PHOTO_W + 4, PHOTO_H + 4, fill=1, stroke=0)
    c.showPage()
    c.save()
    couche.seek(0)
    page.merge_page(PdfReader(couche).pages[0])

    sortie = io.BytesIO()
    writer.write(sortie)
    return sortie.getvalue()


# ---------- Envoi email ----------
def envoyer_mail_sync(pdf: bytes, nom: str, ean: str, quantite: int, date_str: str, dlc: str = ""):
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = MAIL_TO
    msg["Subject"] = f"BNC - EAN {ean} - {date_str}"
    msg.set_content(f"BNC du {date_str}\nEAN : {ean}\nQuantité d'UVC : {quantite}\nDLC/DDM : {dlc or '-'}")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=nom)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ---------- Conversation Telegram ----------
def clavier(*boutons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d)] for t, d in boutons])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["photos"] = []
    await update.message.reply_text(
        "📸 Nouveau BNC.\nEnvoie-moi la ou les photos du produit cassé (3 maximum)."
    )
    return PHOTOS


async def demander_ean(message):
    await message.reply_text(
        "Comment veux-tu donner l'EAN ?",
        reply_markup=clavier(
            ("⌨️ Écrire l'EAN", "ean_txt"),
            ("📷 Photo du code-barres", "ean_photo"),
        ),
    )


async def recevoir_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.user_data["photos"]) >= MAX_PHOTOS_PDF:
        await update.message.reply_text("⚠️ 3 photos maximum, la suivante est ignorée.")
        return PHOTOS
    fichier = await update.message.photo[-1].get_file()
    data = bytes(await fichier.download_as_bytearray())
    ctx.user_data["photos"].append(data)
    n = len(ctx.user_data["photos"])
    if n >= MAX_PHOTOS_PDF:
        await update.message.reply_text("✅ Photo 3 reçue (maximum atteint).")
        await demander_ean(update.message)
        return CHOIX
    await update.message.reply_text(
        f"✅ Photo {n} reçue. Envoie-en une autre (3 max) ou continue.",
        reply_markup=clavier(("➡️ Photos terminées", "photos_ok")),
    )
    return PHOTOS


async def photos_terminees(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not ctx.user_data.get("photos"):
        await q.message.reply_text("Envoie d'abord au moins une photo.")
        return PHOTOS
    await demander_ean(q.message)
    return CHOIX


async def choix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ean_txt":
        await q.message.reply_text("Tape les 13 chiffres de l'EAN :")
        return EAN_TEXTE
    await q.message.reply_text("Envoie la photo du code-barres (bien net, de près) :")
    return EAN_PHOTO


async def ean_texte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ean = re.sub(r"\s", "", update.message.text)
    if not re.fullmatch(r"\d{13}", ean):
        await update.message.reply_text("❌ Il faut exactement 13 chiffres. Réessaie :")
        return EAN_TEXTE
    ctx.user_data["ean"] = ean
    await update.message.reply_text("Quelle est la quantité d'UVC cassés ? (nombre)")
    return QTE


async def ean_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fichier = await update.message.photo[-1].get_file()
    data = bytes(await fichier.download_as_bytearray())
    await update.message.reply_text("🔎 Lecture du code-barres…")
    try:
        ean = await asyncio.to_thread(lire_ean_sync, data)
    except Exception:
        log.exception("Erreur lecture EAN")
        ean = None
    if not ean:
        await update.message.reply_text(
            "😕 Je n'arrive pas à lire le code avec certitude.",
            reply_markup=clavier(
                ("📷 Reprendre la photo", "retry"),
                ("⌨️ Écrire l'EAN", "manual"),
            ),
        )
        return EAN_VALID
    ctx.user_data["ean_candidat"] = ean
    await update.message.reply_text(
        f"🔎 Code lu :\n\n<b><code>{ean}</code></b>\n\nC'est bon ?",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=update.message.message_id,
        reply_markup=clavier(
            ("✅ Valider", "ok"),
            ("📷 Reprendre la photo", "retry"),
            ("⌨️ Écrire l'EAN", "manual"),
        ),
    )
    return EAN_VALID


async def ean_valid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "ok" and ctx.user_data.get("ean_candidat"):
        ctx.user_data["ean"] = ctx.user_data["ean_candidat"]
        await q.message.reply_text("Quelle est la quantité d'UVC cassés ? (nombre)")
        return QTE
    if q.data == "manual":
        await q.message.reply_text("Tape les 13 chiffres de l'EAN :")
        return EAN_TEXTE
    await q.message.reply_text("Envoie la nouvelle photo du code-barres :")
    return EAN_PHOTO


async def quantite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit() or int(txt) <= 0:
        await update.message.reply_text("❌ Entre un nombre entier supérieur à 0 :")
        return QTE
    ctx.user_data["qte"] = int(txt)
    await update.message.reply_text(
        "⏳ Quelle est la DLC / DDM du produit ?\nFormat : JJ/MM/AAAA (ex. 25/12/2026)",
        reply_markup=clavier(("➖ Pas de DLC / DDM", "dlc_none")),
    )
    return DLC


async def dlc_ddm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        ctx.user_data["dlc"] = ""
    else:
        message = update.message
        txt = message.text.strip()
        try:
            datetime.strptime(txt, "%d/%m/%Y")
        except ValueError:
            await message.reply_text(
                "❌ Date invalide. Écris-la au format JJ/MM/AAAA (ex. 25/12/2026) :",
                reply_markup=clavier(("➖ Pas de DLC / DDM", "dlc_none")),
            )
            return DLC
        ctx.user_data["dlc"] = txt
    await message.reply_text(
        "💬 Un commentaire à ajouter ? Écris-le ci-dessous (200 caractères max).",
        reply_markup=clavier(("➖ Aucun commentaire", "com_none")),
    )
    return COMMENT


async def commentaire(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        ctx.user_data["commentaire"] = ""
    else:
        message = update.message
        ctx.user_data["commentaire"] = message.text.strip()[:200]
    await message.reply_text(
        "📅 Quelle est la date d'édition de la fiche ?\nFormat : JJ/MM/AAAA (ex. 16/11/2026)",
        reply_markup=clavier(("📅 Aujourd'hui", "today")),
    )
    return DATE


async def date_bnc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        date_str = datetime.now().strftime("%d/%m/%Y")
    else:
        message = update.message
        date_str = update.message.text.strip()
        try:
            datetime.strptime(date_str, "%d/%m/%Y")
        except ValueError:
            await message.reply_text(
                "❌ Date invalide. Écris-la au format JJ/MM/AAAA (ex. 16/11/2026) :",
                reply_markup=clavier(("📅 Aujourd'hui", "today")),
            )
            return DATE

    ean = ctx.user_data["ean"]
    qte = ctx.user_data["qte"]

    await message.reply_text("📄 Création du PDF…")
    dlc = ctx.user_data.get("dlc", "")
    pdf = await asyncio.to_thread(
        generer_pdf, ctx.user_data["photos"], ean, qte, dlc,
        ctx.user_data.get("commentaire", ""), date_str,
    )
    nom = f"BNC_{ean}_{date_str.replace('/', '-')}.pdf"

    await message.reply_document(
        document=io.BytesIO(pdf), filename=nom,
        caption=f"BNC du {date_str} – EAN {ean} – {qte} UVC",
    )
    try:
        await asyncio.to_thread(envoyer_mail_sync, pdf, nom, ean, qte, date_str, dlc)
        await message.reply_text(f"📧 PDF envoyé à {MAIL_TO}.")
    except Exception:
        log.exception("Erreur envoi mail")
        await message.reply_text("⚠️ PDF créé, mais l'envoi par mail a échoué.")
    await message.reply_text("Tape /bnc pour en faire un nouveau.")
    ctx.user_data.clear()
    return ConversationHandler.END


async def annuler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Annulé. Tape /bnc pour recommencer.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("bnc", start)],
        states={
            PHOTOS: [
                MessageHandler(filters.PHOTO, recevoir_photo),
                CallbackQueryHandler(photos_terminees, pattern="^photos_ok$"),
            ],
            CHOIX: [CallbackQueryHandler(choix, pattern="^(ean_txt|ean_photo)$")],
            EAN_TEXTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ean_texte)],
            EAN_PHOTO: [MessageHandler(filters.PHOTO, ean_photo)],
            EAN_VALID: [CallbackQueryHandler(ean_valid, pattern="^(ok|retry|manual)$")],
            QTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quantite)],
            DLC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dlc_ddm),
                CallbackQueryHandler(dlc_ddm, pattern="^dlc_none$"),
            ],
            COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, commentaire),
                CallbackQueryHandler(commentaire, pattern="^com_none$"),
            ],
            DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_bnc),
                CallbackQueryHandler(date_bnc, pattern="^today$"),
            ],
        },
        fallbacks=[CommandHandler("annuler", annuler), CommandHandler("bnc", start)],
    )
    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
