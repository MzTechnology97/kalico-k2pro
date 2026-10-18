# CFG6 v3.25 — Effetto reale dei 28 parametri CFS (IT)

> **Ambito:** firmware sperimentale K2OH `v323-cfg6`, feature `0xE3`, API2, scheda G32/APP153. 23 impostazioni operative e 5 GET-only. Le funzioni sono **ricostruite dai callsite ARM stock e dalle routine Kalico**; SET/GET confermati sulla CFS reale il 18 ottobre 2026. Non tutti gli effetti meccanici o RFID sono già stati misurati durante una stampa.

## Interpretazione corretta

- Le velocità `1–255 raw` sono valori inviati al controllore del motore hub **senza una conversione convalidata in mm/s o RPM**. Il feeder della bobina invece è acceso/spento. Non trattare `220` come 220 mm/s.
- `ms` è tempo reale in millisecondi; ID8 richiede multipli di 10, ID20/22 multipli di 20, perché la versione stock usa tick interni.
- Un **timeout** cambia il limite temporale che porta al ramo d'errore/recupero: non aumenta la velocità; un **delay** cambia una pausa; un **count** il numero di ripetizioni.
- Tutti gli override risiedono in **RAM volatile** della CFS e Kalico li applica quando si avvia, solo con identità verificata e `auto_apply: True`. Non cambiare parametri durante la stampa.
- La colonna «CM5» è una fotografia dei valori realmente letti il **18/10/2026**, non un insieme di valori raccomandati. I valori di fabbrica sono nella colonna «Stock».
- I cinque parametri **0, 4, 10, 25 e 26** sono in sola lettura. Per gli ID 10/25/26 i siti OEM candidati non risultano eseguiti, per gli ID 0/4 il feeder è ON/OFF.

## Tabella completa

| ID | Parametro `box.cfg` | Stock | Range / unità | CM5 | Modificabile |
|---:|---|---:|---|---:|:---:|
| 0 | `feeder_forward_speed` | 255 | 1–255 (catalogue only) | non operativo | No |
| 1 | `hub_forward_speed` | 100 | 1–255 raw | 100 | Sì |
| 2 | `hub_transition_speed` | 200 | 1–255 raw | 220 | Sì |
| 3 | `hub_insert_speed` | 155 | 1–255 raw | 100 | Sì |
| 4 | `feeder_reverse_speed` | 255 | 1–255 (catalogue only) | non operativo | No |
| 5 | `hub_reverse_speed` | 80 | 1–255 raw | 150 | Sì |
| 6 | `reverse_detooth_enable` | 1 | 0/1 boolean | True | Sì |
| 7 | `hub_transition_wait_ms` | 3200 | 100–10000 ms | 3200 | Sì |
| 8 | `insert_sensor_timeout_ms` | 7000 | 1000–50000 ms, step 10 | 7000 | Sì |
| 9 | `hub_pullback_timeout_ms` | 5000 | 500–15000 ms | 5000 | Sì |
| 10 | `unload_sensor_timeout_ms` | 1000 | 100–5000 ms | GET-only | No |
| 11 | `rfid_seek_speed` | 160 | 1–255 raw | 160 | Sì |
| 12 | `rfid_measure_speed` | 120 | 1–255 raw | 150 | Sì |
| 13 | `rfid_reposition_speed` | 100 | 1–255 raw | 100 | Sì |
| 14 | `rfid_seek_move_ms` | 800 | 100–5000 ms | 800 | Sì |
| 15 | `rfid_seek_settle_ms` | 200 | 10–2000 ms | 200 | Sì |
| 16 | `rfid_post_auth_wait_ms` | 1000 | 100–5000 ms | 1000 | Sì |
| 17 | `rfid_pre_measure_settle_ms` | 400 | 1–2000 ms | 400 | Sì |
| 18 | `rfid_neighbor_detect_delay_ms` | 400 | 50–2000 ms | 400 | Sì |
| 19 | `rfid_neighbor_settle_ms` | 100 | 10–1000 ms | 100 | Sì |
| 20 | `feed_total_timeout_ms` | 25000 | 5000–60000 ms, step 20 | 25000 | Sì |
| 21 | `feed_recovery_timeout_ms` | 3000 | 500–10000 ms | 3000 | Sì |
| 22 | `odometer_stall_timeout_ms` | 500 | 100–2000 ms, step 20 | 500 | Sì |
| 23 | `rfid_neighbor_retry_count` | 3 | 1–10 attempts | 1 | Sì |
| 24 | `reverse_detooth_timeout_ms` | 1000 | 100–10000 ms | 1000 | Sì |
| 25 | `buffer_fill_timeout_ms` | 30000 | 1000–60000 ms | GET-only | No |
| 26 | `transition_detect_timeout_ms` | 300 | 50–5000 ms | GET-only | No |
| 27 | `load_retry_count` | 3 | 1–10 cycles | 1 | Sì |

## Effetto sul comportamento, parametro per parametro

Le descrizioni seguenti sono riferite ai punti specifici del firmware **G32/APP153 v3.25 CFG6**, non a qualunque versione Creality o a OpenCFS.

### ID 00 — `feeder_forward_speed`

Il feeder della bobina è comandato ON/OFF; il valore 255 del catalogo non seleziona una velocità e non accelera il caricamento.

**Limiti e verifica:** Nessun hook di velocità proporzionale; lasciato GET-only. Stock: **255**; range ammesso: **1–255 (catalogue only)**; valore CM5: **non operativo**; SET: **no**.

### ID 01 — `hub_forward_speed`

Comanda la velocità hub nella fase iniziale di spinta in avanti dal CFS. Aumentarla può rendere più rapido questo segmento ma aumentare le sollecitazioni; non modifica l'estrusore della testa.

**Limiti e verifica:** Byte velocità nel blocco configurazione stock, letto dal motore hub. (hook: `stock configuration speed byte`.) Stock: **100**; range ammesso: **1–255 raw**; valore CM5: **100**; SET: **sì**.

### ID 02 — `hub_transition_speed`

Seleziona la velocità hub nella fase di trasferimento lungo il percorso filamento verso buffer e testa. È il controllo CFS più pertinente alla velocità di trasferimento, non un valore in mm/s.

**Limiti e verifica:** Può modificare accelerazione percepita e sforzo nel PTFE; non cambia le fasi di inserimento e la velocità del motore hotend. (hook: `stock configuration speed byte`.) Stock: **200**; range ammesso: **1–255 raw**; valore CM5: **220**; SET: **sì**.

### ID 03 — `hub_insert_speed`

Controlla il motore hub nell'ultima fase di inserimento guidata dai sensori. Non è la velocità con cui gli ingranaggi dell'estrusore spingono il filamento nell'hotend.

**Limiti e verifica:** Aumentarla può peggiorare impuntamenti se il percorso oppone resistenza. (hook: `stock configuration speed byte`.) Stock: **155**; range ammesso: **1–255 raw**; valore CM5: **100**; SET: **sì**.

### ID 04 — `feeder_reverse_speed`

Il feeder della bobina in retromarcia usa un comando ON/OFF, non una regolazione proporzionale di velocità. ID4 non accelera lo scarico.

**Limiti e verifica:** GET-only e non va confuso con ID5. Stock: **255**; range ammesso: **1–255 (catalogue only)**; valore CM5: **non operativo**; SET: **no**.

### ID 05 — `hub_reverse_speed`

Regola la velocità del motore hub mentre il CFS tira il filamento indietro verso la bobina nella fase di scarico. Non regola direttamente la retrazione dell'estrusore sulla testina.

**Limiti e verifica:** Valori elevati possono aumentare slittamento/tensione; verificare con filamenti fragili. (hook: `stock configuration speed byte`.) Stock: **80**; range ammesso: **1–255 raw**; valore CM5: **150**; SET: **sì**.

### ID 06 — `reverse_detooth_enable`

Abilita/disabilita il ramo effettivo di movimento reverse de-tooth (disimpegno/riassetto degli ingranaggi durante retromarcia). Con False il firmware salta quel ramo; non arresta tutte le normali retromarce.

**Limiti e verifica:** Sidecar RAM bit21 + byte50, aggancio con ID24; disabilitarlo può compromettere scarico e recupero. (hook: `0x080108F0`.) Stock: **1**; range ammesso: **0/1 boolean**; valore CM5: **True**; SET: **sì**.

### ID 07 — `hub_transition_wait_ms`

È la pausa dopo la spinta dell'hub e prima che proceda la sequenza verso il buffer. Ridurla elimina attesa, ma può anticipare la transizione prima che il filamento/sensore sia stabile.

**Limiti e verifica:** Usa il delay stock; non è il limite massimo dell'intero caricamento. (hook: `0x08011116`.) Stock: **3200**; range ammesso: **100–10000 ms**; valore CM5: **3200**; SET: **sì**.

### ID 08 — `insert_sensor_timeout_ms`

È la finestra massima concessa al sensore/percorso d'inserimento durante la fase di caricamento. Se scade, il firmware segue il ramo di errore/recupero previsto; non accelera il movimento.

**Limiti e verifica:** CFG in ms; il firmware converte in tick da 10 ms. (hook: `0x080112CA`.) Stock: **7000**; range ammesso: **1000–50000 ms, step 10**; valore CM5: **7000**; SET: **sì**.

### ID 09 — `hub_pullback_timeout_ms`

È il limite temporale del percorso di richiamo verso il buffer durante lo scarico. Un valore troppo basso può provocare timeout prima che il filamento sia rientrato.

**Limiti e verifica:** Il wrapper sostituisce il timeout stock al punto di chiamata della fase, non la velocità di retromarcia. (hook: `0x08011CB6`.) Stock: **5000**; range ammesso: **500–15000 ms**; valore CM5: **5000**; SET: **sì**.

### ID 10 — `unload_sensor_timeout_ms`

Voce descrittiva di un timeout scarico stock il cui percorso individuato non viene eseguito in questa variante. Cambiarne il valore non avrebbe effetti dimostrati.

**Limiti e verifica:** SET/RESET bloccati. Stock: **1000**; range ammesso: **100–5000 ms**; valore CM5: **GET-only**; SET: **no**.

### ID 11 — `rfid_seek_speed`

Velocità del motore hub mentre il firmware muove la bobina per localizzare il tag RFID; non influisce sull'avanzamento filamento verso la testina.

**Limiti e verifica:** Passa il valore alla chiamata stock hub-speed `0x0801E164` nella ricerca tag. (hook: `0x08012A9E`.) Stock: **160**; range ammesso: **1–255 raw**; valore CM5: **160**; SET: **sì**.

### ID 12 — `rfid_measure_speed`

Velocità usata quando la bobina viene fatta ruotare/mossa durante la misura RFID. Cambiarla modifica il moto della fase di lettura, non la portata dell'estrusore.

**Limiti e verifica:** A velocità elevata la qualità della lettura va confermata con tag reali. (hook: `0x08012AC0`.) Stock: **120**; range ammesso: **1–255 raw**; valore CM5: **150**; SET: **sì**.

### ID 13 — `rfid_reposition_speed`

Velocità hub per riposizionare la bobina tra i passaggi RFID, ad esempio dopo una misura non conclusiva.

**Limiti e verifica:** Non controlla il caricamento finale del filamento. (hook: `0x08012E8A`.) Stock: **100**; range ammesso: **1–255 raw**; valore CM5: **100**; SET: **sì**.

### ID 14 — `rfid_seek_move_ms`

Durata del movimento di ricerca del tag RFID nel ciclo stock. Allungandola, si prolunga il movimento di ricerca; non aumenta la velocità.

**Limiti e verifica:** Sostituisce la costante temporale a `0x08012A76`; attenzione a bobine ai finecorsa. (hook: `0x08012A76`.) Stock: **800**; range ammesso: **100–5000 ms**; valore CM5: **800**; SET: **sì**.

### ID 15 — `rfid_seek_settle_ms`

Pausa successiva a un movimento di ricerca RFID prima del passo successivo. Ridurla può abbreviare il ciclo ma togliere tempo alla stabilizzazione della bobina.

**Limiti e verifica:** È un delay, non una velocità o un numero di tentativi. (hook: `0x08012AAE`.) Stock: **200**; range ammesso: **10–2000 ms**; valore CM5: **200**; SET: **sì**.

### ID 16 — `rfid_post_auth_wait_ms`

Attesa stock nella transizione successiva a un passaggio di autenticazione/ritorno RFID, prima delle operazioni successive. Modifica un delay, non la velocità di inversione.

**Limiti e verifica:** L'hook agisce sulla chiamata di delay a `0x08012E7A`; l'esatta necessità fisica va confermata con RFID reali. (hook: `0x08012E7A`.) Stock: **1000**; range ammesso: **100–5000 ms**; valore CM5: **1000**; SET: **sì**.

### ID 17 — `rfid_pre_measure_settle_ms`

Pausa di 400 ms effettivamente presente prima della lettura/misura RFID. Ridurla può diminuire il tempo totale ma anticipare la lettura durante le vibrazioni.

**Limiti e verifica:** Hook corretto a `0x08012A60`; non il separato delay stock da 10 ms. (hook: `0x08012A60`.) Stock: **400**; range ammesso: **1–2000 ms**; valore CM5: **400**; SET: **sì**.

### ID 18 — `rfid_neighbor_detect_delay_ms`

Costante temporale del movimento/posizionamento usato nella ricerca del tag su una bobina adiacente; non è un timeout di carico filamento.

**Limiti e verifica:** Il nome contiene 'delay', ma nel codice la costante entra nel ciclo di movimento; non assumere sia una pausa completamente statica. (hook: `0x0801304A`.) Stock: **400**; range ammesso: **50–2000 ms**; valore CM5: **400**; SET: **sì**.

### ID 19 — `rfid_neighbor_settle_ms`

Pausa dopo il movimento di ricerca sullo slot/bobina vicina; permette stabilizzazione prima di continuare o riprovare.

**Limiti e verifica:** Hook nella chiamata delay `0x08013078`. (hook: `0x08013078`.) Stock: **100**; range ammesso: **10–1000 ms**; valore CM5: **100**; SET: **sì**.

### ID 20 — `feed_total_timeout_ms`

Timeout complessivo della fase 4 di alimentazione verso il percorso testa. Se il trasferimento è troppo lento può causare il percorso di timeout; aumentarlo concede più tempo, non maggiore velocità.

**Limiti e verifica:** Il firmware converte ms/20 per il contatore stock; hook `0x08032C72`. (hook: `0x08032C72`.) Stock: **25000**; range ammesso: **5000–60000 ms, step 20**; valore CM5: **25000**; SET: **sì**.

### ID 21 — `feed_recovery_timeout_ms`

Tempo massimo concesso alla manovra di recupero dopo una difficoltà/errore nella fase 4. Modifica quando il recupero fallisce o riprova, non la velocità di avanzamento.

**Limiti e verifica:** Due rami stock condividono questo parametro (`0x08032D46`, `0x08032DAC`). (hook: `0x08032D46 + 0x08032DAC`.) Stock: **3000**; range ammesso: **500–10000 ms**; valore CM5: **3000**; SET: **sì**.

### ID 22 — `odometer_stall_timeout_ms`

Tempo per cui l'odometro può non progredire durante alimentazione prima di considerare il movimento in stallo. Valori troppo bassi possono produrre falsi stall; troppo alti ritardano la rilevazione.

**Limiti e verifica:** Non cambia quanti millimetri vengono contati; solo soglia temporale, convertita in tick da 20 ms. (hook: `0x08032CEC`.) Stock: **500**; range ammesso: **100–2000 ms, step 20**; valore CM5: **500**; SET: **sì**.

### ID 23 — `rfid_neighbor_retry_count`

Numero di passaggi/tentativi nel ciclo di ricerca RFID della bobina adiacente. Ridurlo a 1 abbrevia il ciclo ma riduce la probabilità di trovare tag difficili.

**Limiti e verifica:** Confronto stock su `r4` al sito `0x0801307C`, non retry globale di tutti gli errori CFS. (hook: `0x0801307C`.) Stock: **3**; range ammesso: **1–10 attempts**; valore CM5: **1**; SET: **sì**.

### ID 24 — `reverse_detooth_timeout_ms`

Soglia temporale del ramo reverse de-tooth. Coordina il momento/condizione in cui il firmware esegue il disimpegno; non modifica la velocità di scarico.

**Limiti e verifica:** Interagisce con ID6: disabilitare ID6 forza il salto del ramo; hook `0x080108F0`. (hook: `0x080108F0`.) Stock: **1000**; range ammesso: **100–10000 ms**; valore CM5: **1000**; SET: **sì**.

### ID 25 — `buffer_fill_timeout_ms`

Timeout del riempimento buffer presente nel catalogo, ma senza percorso stock raggiungibile agganciato. Non agisce sul tempo reale di riempimento nella CFG6.

**Limiti e verifica:** Non esposto a SET/RESET. Stock: **30000**; range ammesso: **1000–60000 ms**; valore CM5: **GET-only**; SET: **no**.

### ID 26 — `transition_detect_timeout_ms`

Timeout di rilevamento transizione presente solo nel catalogo, senza un aggancio stock operativo in questa build.

**Limiti e verifica:** Non esposto a SET/RESET. Stock: **300**; range ammesso: **50–5000 ms**; valore CM5: **GET-only**; SET: **no**.

### ID 27 — `load_retry_count`

Numero dei cicli di pretensione avanti/indietro nel percorso di caricamento. Non rappresenta il numero totale di tentativi di caricare una bobina dopo qualsiasi guasto.

**Limiti e verifica:** L'hook modifica il vero confronto del ciclo a `0x08012616`; valore 1 riduce le ripetizioni stock da 3 a 1. (hook: `0x08012616`.) Stock: **3**; range ammesso: **1–10 cycles**; valore CM5: **1**; SET: **sì**.

## Velocità di carico/scarico: CFS contro testina

**Il motore hub CFS** usa prevalentemente ID2 (`hub_transition_speed`) durante il trasferimento verso la testa, ID3 (`hub_insert_speed`) durante l'inserimento finale, ID1 (`hub_forward_speed`) per la spinta iniziale e ID5 (`hub_reverse_speed`) in scarico. Gli ID11–13 controllano *movimenti RFID*, non il trasferimento normale.

**L'estrusore sulla testa**, invece, è regolato dalla sezione `[box]` di Kalico, non da questi 28 ID: `hotend_feed_speed` è una portata volumetrica in mm³/s; `retract_velocity` ed `external_feed_velocity` sono feedrate in mm/min. Questi parametri agiscono su una cinematica diversa da quella dei feeder/hub CFS.

## Come e cosa è stato verificato

- Confermati da sorgente: tabella Kalico, valori di default, confini e indirizzi ARM originali; differenziale Unicorn dei wrapper, dei flag e della sequenza tipo 4.
- Confermati su hardware: identità `v323-cfg6`, 23 scritture SET da `box.cfg` all'avvio, GET di tutti i 28 ID e corrispondenza 23/23. **Non** sono stati eseguiti homing Z, spostamenti, carico/scarico o cicli RFID nella verifica.
- Ancora da verificare fisicamente: reale dinamica dei 15 hook avanzati, letture RFID su tag diversi, stabilità prolungata RS-485 e contatore residuo type 4 durante una stampa.
- **Indicazione prudenziale:** tenere un backup della CFG5T; provare gli override RFID e i timeout singolarmente, registrando i risultati prima di aumentarli o ridurli.

## Riferimenti al codice

[builder](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/build-v3_23.py) · [ARM wrappers](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/handler-v3_23.S) · [automated tests](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/firmware/v3.25-cfg6-lab/test_all_offline.py)

[Guida di integrazione Kalico](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/docs/CFS_RUNTIME_CFG6_IT.md) · [English version](CFS_RUNTIME_CFG6_PARAMETERS_EN.md)
