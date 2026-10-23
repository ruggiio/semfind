# semfind

Ricerca semantica locale di tutti i file del PC, con uno **Spotlight in stile macOS** per GNOME.
Cerchi per significato ("come si calibra il sensore", "foto in bicicletta") e trovi documenti,
pagine di PDF, immagini e screenshot, anche se non contengono le parole esatte. Tutto resta sul
PC: nessun servizio esterno, nessun dato inviato.

Il modello è [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) di Google
(ottobre 2026, Apache 2.0): mette testo e immagini nello stesso spazio vettoriale, quindi una
domanda scritta trova anche foto e PDF scansionati.

## Come funziona

```
            ┌───────────────────── indicizzazione ─────────────────────┐
 file ──►   │ giro notturno (GPU, torch)        watcher inotify (CPU)  │ ──► SQLite
            │ tutto: testo, immagini, PDF       file nuovi, subito     │     FTS5 + sqlite-vec
            └──────────────────────────────────────────────────────────┘     (vettori a 256 dim.)
                                                                                 │
 Super+Spazio ──► Spotlight (GTK4) ──► llama-server (GGUF Q8_0, CPU) ──► ricerca ibrida
 cerca "…"   ──►  terminale       ──┘   vettore della query in ~20 ms     (vettori + parole + nomi)
```

- **Indicizzazione completa** (`semfind index`): un timer di systemd la lancia al massimo una
  volta al giorno, **solo con l'alimentatore collegato**, e si ferma se lo stacchi. È
  incrementale: rilegge solo i file nuovi o modificati.
- **File nuovi** (`semfind-watch`): appena scarichi o salvi un file nelle cartelle principali, il
  nome è cercabile dopo ~4 s e i documenti di testo piccoli vengono indicizzati subito, sulla CPU.
  File grandi, immagini e PDF scansionati aspettano il giro notturno sulla GPU.
- **Ricerca**: unisce somiglianza dei vettori, parole chiave (BM25) e nomi dei file con
  Reciprocal Rank Fusion; i duplicati (stesso nome e dimensione) diventano un solo risultato.

## Requisiti

- Linux x86_64 con **GNOME** (Wayland), Python ≥ 3.11, GTK4 e libadwaita per Python:
  `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`
- Facoltativo: `gnome-sushi` (anteprima rapida con Ctrl+Y), `papers` (apre i PDF alla pagina trovata)
- **GPU NVIDIA consigliata** per l'indicizzazione: ~27 pezzi di testo/s contro ~2 sulla CPU.
  Senza GPU funziona tutto, ma il primo indice può richiedere molte ore.
- Spazio: ~7 GB per gli ambienti Python e i modelli, più l'indice (~270 MB ogni 60.000 pezzi).

## Installazione

```bash
git clone <questo repository> ~/semfind     # o dove preferisci
cd ~/semfind
./install.sh --bind-super-space              # senza l'opzione non tocca le scorciatoie
semfind index                                # primo indice, meglio sotto carica
```

L'installer non usa root e si può rilanciare: crea i due ambienti Python (`.venv` per
l'indicizzatore con torch, `.venv-ui` per Spotlight con il GTK di sistema), scarica llama.cpp e
i modelli, installa comandi in `~/.local/bin`, servizi utente di systemd e attivazione DBus.
Opzioni: `--cpu-torch` (torch senza CUDA; scelto da solo se non c'è `nvidia-smi`),
`--no-services`. Per togliere tutto: `./uninstall.sh` (aggiungi `--purge` per cancellare indice e
configurazione).

`--bind-super-space` assegna **Super+Spazio** a Spotlight. Se hai un solo layout di tastiera
toglie quella combinazione al cambio layout, altrimenti la lascia e ti avvisa.

## Uso

**Spotlight** (Super+Spazio)

| Tasto | Azione |
|---|---|
| ↑ ↓ / Invio | scegli e apri (i PDF si aprono alla pagina trovata, se usi Papers) |
| Ctrl+Invio | mostra il file in Nautilus |
| Ctrl+Y | anteprima rapida con Sushi; dentro l'anteprima le frecce scorrono i risultati |
| Ctrl+1 / 2 / 3 | solo app / file / immagini (anche con i pulsanti nella barra) |
| Esc | cancella la ricerca; un secondo Esc chiude |

Trova anche le app e fa da calcolatrice (`sqrt(2)*10`, Invio copia il risultato).

**Terminale**

```bash
cerca "come si calibra il sensore"     # ricerca ibrida
cerca "bici" --img                     # solo immagini e PDF scansionati
cerca "kalman" -t pdf,md -i ~/Documenti # filtri per tipo e cartella
cerca --name relazione finale          # solo nomi dei file
cerca "…" --json                       # per script
semfind stats                          # stato dell'indice
semfind index                          # aggiornamento manuale
```

## Configurazione

`~/.config/semfind/config.toml` (esempio commentato in `config.example.toml`): cartelle da
indicizzare, esclusioni, cartelle osservate in tempo reale (di default quelle XDG: Scaricati,
Scrivania, Documenti, Immagini), tipi di file. Le cartelle nascoste (`.ssh`, `.gnupg`, profili dei
browser, …) sono sempre escluse, così come `node_modules`, `venv`, `build` e simili.

## Risorse

Misurate su un portatile con Xeon W-11955M, 32 GB e RTX A2000 (4 GB):

| | |
|---|---|
| Primo indice: 62.000 pezzi di testo + 3.300 immagini | ~44 min sulla GPU |
| Giro incrementale senza novità | ~2,5 s |
| RAM sempre occupata (llama-server + Spotlight + watcher) | ~260 MB propri, più 290 MB di modello mappato (cache che il kernel libera se serve) |
| Una ricerca | ~60 ms in Spotlight, ~0,3 s da terminale |

Il percorso delle query non usa torch: torch sulla CPU costerebbe 2,4 GB di RAM. llama.cpp con il
GGUF Q8_0 produce gli stessi vettori dell'indice (coseno ≥ 0,997).

## File

```
semfind/   config.py   default e config.toml          store.py   schema SQLite, FTS5, sqlite-vec
           extract.py  testo da PDF/docx/pptx/…       embed.py   EmbeddingGemma 2 con torch (indicizzazione)
           index.py    scansione e giro completo      qembed.py  vettori via llama-server (query, watcher)
           watch.py    inotify, file nuovi            search.py  ricerca ibrida + RRF
           cli.py      comandi semfind / cerca
ui/        spotlight.py + spotlight.css   finestra GTK4 precaricata
systemd/   template dei servizi utente (@PREFIX@ e @DATA@ li sostituisce install.sh)
bin/, dbus/  template dei comandi e dell'attivazione DBus
```

## Problemi noti

- Niente sfocatura dietro il pannello: su GNOME una normale app non può sfocare ciò che sta sotto.
  Servirebbe un'estensione di GNOME Shell.
- Spotlight è una finestra trasparente massimizzata, non a schermo intero: per la specifica
  xdg-shell sotto una finestra fullscreen il compositor deve nascondere tutto il resto.
- Se il tema GTK installa un `~/.config/gtk-4.0/gtk.css`, Spotlight lo scavalca con priorità più
  alta. Se qualcosa appare strano, il primo sospettato è quello.
- Con l'anteprima aperta, cliccare su un'altra finestra non chiude Spotlight.
- Log: `journalctl --user -u semfind-embed -u semfind-spotlight -u semfind-watch -u semfind-index`
