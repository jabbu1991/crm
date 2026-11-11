# Real View Android app

A small WebView app that opens https://crm.realviewcreativestudio.in/ full screen and adds Bluetooth printing
(`window.RVApp.printEscpos(base64)`), file downloads (bill PDF, Excel), photo upload and camera (barcode scan).
Online orders: the app checks every 15 s (OrderWatch service, also when closed / locked) and rings with a
lock-screen notification until the order is confirmed; started by the website bell (`RVApp.watchOrders(true)`).
The screens are the website, so website changes need no new APK. Only changes in this folder need a new APK.

Build: GitHub → Actions → "Build Android app" → Run workflow (secret `APK_KEY_PASSWORD` needed).
The APK is in the run's "Artifacts". `release.keystore.enc` is the signing key, locked with that password —
keep the password safe: without it, new APKs cannot update the installed app.
