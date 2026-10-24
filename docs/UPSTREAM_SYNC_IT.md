# K2 Pro OpenHost: seguire gli upstream

`k2-pro-openhost` è un fork di
[Jacob10383/kalico](https://github.com/Jacob10383/kalico), che a sua volta è
un fork di [KalicoCrew/kalico](https://github.com/KalicoCrew/kalico). Tutti e
due vanno avanti: Jacob aggiunge lavoro per la K2 e ogni tanto unisce Kalico,
Kalico corregge e aggiunge funzioni. Questa pagina descrive come si trovano,
si verificano e si portano qui i loro nuovi commit.

Niente in questo processo fa merge da solo: il sistema riporta, le persone
decidono.

## Il report settimanale

Ogni lunedì alle 06:00 UTC (e a richiesta dalla scheda Actions) il workflow
[Upstream watch](https://github.com/MzTechnology97/kalico-k2pro/actions/workflows/upstream-watch.yaml):

1. scarica il `main` dei due upstream;
2. esegue
   [`scripts/upstream_sync.py`](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/scripts/upstream_sync.py),
   che elenca i commit upstream che `k2-pro-openhost` non ha;
3. prova ogni merge in memoria (`git merge-tree`) ed elenca i file in
   conflitto;
4. scrive il report nella issue aperta con l'etichetta `upstream-sync` (la
   crea la prima volta);
5. per un upstream che si unisce senza conflitti, fa il merge nel runner,
   esegue i test di Klippy con Python 3.11, la versione del CM5 di
   riferimento, e commenta il risultato nella issue.

Il report esclude:

- i commit la cui modifica è già qui con un altro hash (cherry-pick, port);
- i commit di Kalico già presenti nel `main` di Jacob: arrivano con Jacob;
- i commit con una decisione registrata (vedi sotto).

## Categorie

Ogni commit in sospeso riceve una categoria, in base ai suoi file:

| | Categoria | Significato |
|---|---|---|
| 🔴 | movimento / temporizzazione / MCU | Tocca la generazione dei passi, la cinematica, l'homing, il collegamento con le MCU (`klippy/chelper/`, `toolhead.py`, `stepper.py`, `mcu.py`, `serialhdl.py`, `clocksync.py`, `motion_queuing.py`…) o i sorgenti MCU comuni in `src/`. |
| 🟡 | tocca file nostri | Un file che `k2-pro-openhost` ha modificato dall'ultimo punto in comune con quell'upstream. Va portato a mano. |
| 🟢 | file mai modificati da noi | Solo file ancora uguali a quelli upstream. |
| ⚪ | altro | Solo documentazione, CI, configurazioni di esempio o schede MCU che nessuna macchina K2-OpenHost usa (stm32, rp2040, avr…). |

Le sigle nell'ultima colonna:

- **K2**: moduli K2 (`box*`, `motor_control`, `prtouch`, `serial_485`,
  `z_align`…) o `config/k2/`;
- **MCU**: sorgenti MCU comuni;
- **ours**: un commit rosso su file nostri.

Il rosso conta perché le MCU della K2 usano il firmware Creality, non il
`src/` di questo albero. Una modifica host che si aspetta un comportamento
nuovo delle MCU può passare la CI e rompersi lo stesso sulla stampante. Per
lo stesso motivo l'immagine della CI è bloccata (vedi `ci-build_test.yaml`).

## Dal report a `k2-pro-openhost`

Si lavora a lotti, su un branch e con una pull request, mai direttamente su
`k2-pro-openhost`.

| Categoria | Cosa serve prima del merge |
|---|---|
| ⚪ | Niente: di solito si scarta o si prende col lotto successivo. |
| 🟢 | CI verde. |
| 🟡 | Una pull request di allineamento con test per il comportamento portato (come la #39, `box: catch up with Jacob10383 k2-plus`). |
| 🔴 | CI verde, poi sulla stampante di riferimento, a macchina ferma: riavvio di Klipper, lettura di `klippy.log`, una stampa breve. Solo dopo il merge. |

Per le modifiche di Kalico che Jacob ha già unito, meglio prenderle dal `main`
di Jacob, perché le ha già riconciliate con il codice K2. Un commit di Kalico
si prende direttamente quando Jacob non l'ha ancora unito e corregge qualcosa
che ci serve.

## Regole per un merge di allineamento

Un merge del `main` di Jacob porta anche le sue versioni dei moduli K2: da
"Add K2 extras" (19/10/2026) il suo albero contiene `box*.py`,
`motor_control.py`, `prtouch.py`, `serial_485.py`, `z_align.py` e gli altri,
scritti per la K2 Plus. Sul branch di allineamento:

1. Merge con `git merge --no-ff --no-commit`.
2. Si tengono i nostri, anche dove git ha unito da solo: `README.md`,
   `config/k2/` e i moduli K2 (i percorsi K2 in `scripts/upstream_sync.py`).
3. Non si prendono i file di configurazione K2 Plus di Jacob
   (`config/k2/box.cfg`, `macros.cfg`, `motor_control.cfg`,
   `start_print.cfg`), la `klippy/chelper/c_helper.so` già compilata né i
   moduli per hardware che la K2 Pro non ha (`belt_mdl.py`). Mai copiare
   valori della K2 Plus (dimensioni del piano, cestino, pin) nel profilo
   K2 Pro.
4. Si controlla che i nostri moduli K2 trovino ancora ogni nome del nucleo
   che usano: il nucleo sposta parti interne (a ottobre
   `toolhead.*_flush_time` è passato a `motion_queuing`,
   `stepper_enable.motor_debug_enable` è diventato `set_motors_enable`). Si
   adattano nella stessa pull request.
5. Si elenca cosa Jacob ha cambiato nei suoi moduli K2 e vale la pena avere:
   diventano port separati, con test.

## Dizionari delle MCU

Le MCU della K2 usano il firmware Creality, i cui dizionari dati esistono
solo sulla stampante. `[save_mcu_dictionaries]` (in
`config/k2/macros/system.cfg`) aggiunge `SAVE_MCU_DICTIONARIES`. A stampante
ferma lo si esegue e si copiano `~/printer_data/mcu_dictionaries/*.dict` in
`test/dictionaries/k2pro/`. Con questi file un test può caricare il profilo
K2 Pro nella CI e accorgersi di una modifica upstream che lo rompe. Vanno
salvati di nuovo dopo un aggiornamento firmware.

## Registrare le decisioni

Ogni commit in sospeso finisce con una decisione, così il report successivo
mostra solo il lavoro nuovo. Si registra con lo script e si committa la
modifica a `.github/upstream-sync.json` insieme alla pull request che ha
fatto il lavoro:

```
python3 scripts/upstream_sync.py decide <commit>... --decision taken --pr 95 \
    --note "core fix, merged with the October batch"
python3 scripts/upstream_sync.py decide <commit> --decision ported --pr 96 \
    --note "box.py: ported by hand"
python3 scripts/upstream_sync.py decide <commit> --decision skipped \
    --note "stm32 SPI: board not used on K2-OpenHost"
```

`taken` vuol dire unito così com'è, `ported` riscritto per il nostro codice,
`skipped` non voluto, con il motivo. I commit che arrivano con un merge
dell'upstream spariscono da soli dal report e non serve una decisione.

## Eseguirlo in locale

```
python3 scripts/upstream_sync.py report --fetch --markdown report.md
```

`--fetch` aggiunge i remote `jacob` e `kalico` se mancano e li scarica.
`--ref jacob=upstream/main` usa un altro ref, `--no-merge` salta il merge di
prova, `--json` scrive i dati grezzi. Lo script richiede solo Python 3 e git
2.38 o successivo.
