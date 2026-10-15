# Real View CRM: online ആക്കാനുള്ള steps

ചെലവ്: മാസം ഏകദേശം **$7.25 (₹650–700)**. Website free.

## 1. Card ചേർക്കുക
- https://dashboard.render.com/billing → **Add payment method** → card + OTP
- Card-ൽ **International transactions ON** ആയിരിക്കണം (bank app → Cards → International usage)

## 2. CRM ഉണ്ടാക്കുക
1. Render → മുകളിലെ **+** → **Blueprint**
2. **jabbu1991/crm** → **Connect**
3. Blueprint Name: `realview` · Branch: `claude/marketing-agency-crm-y3shvh`
4. **realview-crm** (Starter + 1 GB disk) മാത്രം കാണിക്കും → **Deploy Blueprint**
5. 3–5 മിനിറ്റ്. പച്ച **Live** ആകുന്നത് വരെ കാത്തിരിക്കുക

## 3. Super Admin ഉണ്ടാക്കുക
1. **realview-crm → Environment** → `CRM_SETUP_KEY`-ന്റെ 👁 അമർത്തി key copy ചെയ്യുക
2. **https://realview-crm.onrender.com** തുറക്കുക
3. Setup key paste ചെയ്യുക. Agency: Real View Creative Studio, പേര്, username, password (8+)
4. ⚠️ Password എവിടെയെങ്കിലും സുരക്ഷിതമായി എഴുതി വെക്കുക

## 4. crm.realviewcreativestudio.in connect ചെയ്യുക
1. Render: **realview-crm → Settings → Custom Domains → Add** → `crm.realviewcreativestudio.in`
2. Hostinger: **hpanel → Domains → realviewcreativestudio.in → DNS / Nameservers → Add record**
   - Type: **CNAME** · Name: **crm** · Target: **realview-crm.onrender.com** (Render കാണിക്കുന്നത്) · TTL: **300**
   - പഴയ `@` (A), `www` (CNAME) records **മാറ്റരുത്**
3. 15–30 മിനിറ്റ് കഴിഞ്ഞ് Render-ൽ **Verify** → Verified + Certificate Issued
4. **https://crm.realviewcreativestudio.in** തുറക്കുക

## 5. ആദ്യ client
Client Organizations → **New client organization** → company, code, admin name/username → **invite link** → WhatsApp വഴി client-ന് അയക്കുക

## പ്രശ്നം വന്നാൽ
- Deploy **Failed**: realview-crm → **Logs** screenshot എടുക്കുക
- Card reject: international ON ആണോ നോക്കുക, അല്ലെങ്കിൽ credit card ഉപയോഗിക്കുക
- നിർത്തണമെങ്കിൽ: realview-crm → Settings → **Suspend** (data സുരക്ഷിതം)
- Backup: Render disk-ന്റെ daily snapshot. ആഴ്ചയിൽ ഒരിക്കൽ CRM-ൽ **Export → Download everything (ZIP)** എടുത്ത് സൂക്ഷിക്കുക

## പിന്നീട് മാറ്റങ്ങൾക്ക്
പുതിയ Claude Code session → repository **jabbu1991/crm** → ഇങ്ങനെ പറയുക:
> "Real View CRM-ൽ (app.py, templates/) ഈ മാറ്റം വരുത്തി, tests (`python -m pytest -q`) pass ആക്കി, `claude/marketing-agency-crm-y3shvh` branch-ലേക്ക് push ചെയ്യൂ: …"

Push ചെയ്താൽ CRM-ഉം website-ഉം 2–5 മിനിറ്റിനുള്ളിൽ സ്വയം update ആകും.
