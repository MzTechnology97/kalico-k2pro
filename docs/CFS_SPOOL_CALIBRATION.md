# Calibrazione della geometria bobine CFS

Questa guida usa il firmware **CFG6 SGF1/RH1 R3** e l'extra Kalico
`box_spool_profiles`. Kalico identifica il vendor dopo la decodifica RFID,
carica la geometria in RAM nel CFS e associa la misura all'UID della bobina.
Il CFS calcola quindi la percentuale; Kalico conserva cache e consumo come
riferimento quando il CFS perde la RAM.

## Configurazione Bambu di riferimento

Il file pronto all'uso è
[`config/k2/macros/box_spool_profiles.cfg`](https://github.com/MzTechnology97/kalico-k2pro/blob/k2-pro-openhost/config/k2/macros/box_spool_profiles.cfg).
Il profilo usa la circonferenza piena misurata sulla PETG HF da 1 kg e un
**nucleo effettivo corretto al primo esaurimento reale**. Il cartone vuoto
misurato a circa 83 mm resta un riferimento fisico; non coincide con il
coefficiente effettivo del modello CFS.

```ini
[box_spool_profiles]
serial: serial485
enabled: True
single_cycle_rfid: True
handoff_wait_ms: 3000
handoff_lease_ms: 10000
reconcile_interval: 5.0
vendor_geometries:
    bambu = 1, 284.7, 508.3
```

I parametri dopo l'ID sono **circonferenze in millimetri**:

| Parametro | Valore | Provenienza |
|---|---:|---|
| Diametro esterno del cartone vuoto | circa 83 mm | misura dell'operatore |
| Circonferenza del nucleo effettivo | 284,7 mm | ricavata da 19,655 m consumati fino a esaurimento su 327 m nominali |
| Circonferenza piena | 508,3 mm | misura CFS 508,253 mm sulla bobina nuova |

Il valore pieno è un riferimento misurato, non il diametro della flangia.
Verificare i riferimenti per supporti, formati di refill o avvolgimenti diversi.
Il materiale e il colore non scelgono una geometria distinta; la lunghezza
nominale resta per UID/slot e proviene dal tag, dal profilo filamento o dai
valori materiale già disponibili.

## Installazione

1. Installare il firmware R3 per **CFS G32 / applicazione cfs0_000_153**.
   Il BIN verificato ha SHA-256
   `a6dec25c74e7d259eec0387193548a0c3cc78ec9fc4ddc35824f0fe0965385b8`.
   Nel Helper il menu 40 presenta R3 come prima voce. L'identità runtime
   `v323-cfg6` da sola non distingue R2 da R3.
2. Copiare il file nella configurazione attiva come
   `~/printer_data/config/macros/box_spool_profiles.cfg`. Il profilo generico
   K2 contiene l'include commentato; il riferimento CM5 lo include già.
3. Dopo gli include di `box.cfg` e dei decoder RFID, includerlo **una sola volta**:

   ```ini
   [include macros/box_rfid_diag.cfg]
   [include macros/box_rfid_bambu.cfg]
   [include macros/box_spool_profiles.cfg]
   ```

   `box_rfid_mifare.cfg` è opzionale per gli altri decoder. Non duplicare la
   sezione `[box_spool_profiles]` includendo anche l'esempio standalone.
4. Riavviare Klipper a stampante e CFS inattivi. Nello stato Moonraker
   `box_spool_profiles`, verificare vendor `bambu`, nessun errore e
   `handoff.cfs["1"].supported/configured` entrambi veri.

Il file `config/cfs-spool-profiles-example.cfg` contiene gli stessi riferimenti
ma resta disabilitato finché non viene esplicitamente abilitato.

## Eseguire una calibrazione

### 1. Misurare il nucleo vuoto

Misurare il diametro esterno del cartone o cilindro **su cui poggia il primo
strato di filamento**. Il foro centrale e la flangia esterna non sono quel
riferimento. Calcolare:

```text
C_nucleo = pi greco * D_nucleo
83 mm -> 260,752 mm -> 260,8 mm
```

Una bobina quasi vuota contiene ancora materiale: può servire come controllo
del risultato, ma la sua circonferenza non rappresenta il nucleo nudo. Nel
collaudo, usarla come limite inferiore dava 1%; sostituendo quel riferimento
con il cartone da 83 mm la stessa misura dà circa 12%.

### 2. Acquisire la bobina piena

Usare una bobina nota come nuova e con la geometria che si vuole calibrare.
La lettura manuale richiede il filamento scaricato dalla testina, hub libero,
CFS inattivo e nessuna stampa in corso. I numeri SLOT sono **zero-based**:
SLOT=1 indica lo slot fisico 2, SLOT=3 lo slot fisico 4.

```gcode
_BOX_RFID_READ_SLOT SLOT=1
BOX_SPOOL_GEOMETRY_STATUS ADDRESS=1 SLOT=1 DETAIL=1
```

Il primo comando avvia un ciclo meccanico; il secondo legge soltanto la misura
salvata. Annotare `raw_circumference_mm`, UID, generazione e `flags`.
Per il percorso API7 coerente il risultato atteso è **flags=0x13**:

- bit 0: misura valida, esito riuscito e circonferenza nel range ammesso;
- bit 1: cattura API7 fresca e completa;
- bit 4: i due UID osservati coincidono con l'UID della misura.

`flags=0x02` non basta per associare una geometria. `record_byte75=0` può
invece essere normale con R3: sono i due getter a recuperare gli UID, senza
riscrivere quel metadato. Se la misura non è valida, verificare immagine e
stato del ciclo prima di calibrare.

Usare la circonferenza grezza acquisita prima del clamp OEM. Nel riferimento:
`raw_circumference_mm=508.253`, quindi `C_pieno=508.3`.

### 3. Salvare il profilo vendor

La sintassi è:

```text
vendor = ID_profilo, C_nucleo_mm, C_pieno_mm
```

Gli ID disponibili sono 1..4, univoci per vendor. `bambu`, `Bambulab` e
`Bambu Lab` vengono normalizzati allo stesso vendor. Per aggiungerne un altro
serve che un decoder fornisca la sua identità: il solo nome commerciale del
filamento non certifica la geometria della bobina.

Modificare `vendor_geometries` nel file attivo e riavviare Klipper a CFS
inattivo. Non occorre riflashare il firmware per cambiare questi parametri.
Kalico carica il catalogo in RAM, invia la lunghezza nominale per UID e usa
BIND per ricalcolare una misura ancora valida **solo se il runtime non ha
ancora consumo protetto**. Una revisione diversa con consumo gia contato
viene rifiutata con stato 7: non azzerare i contatori per aggirare il vincolo.
In quel caso conservare la stima e acquisire una nuova misura a CFS inattivo.
Se conserva la misura con used_mm=0, il ricalcolo non richiede un nuovo giro. Se ha perso la RAM, la cache continua a
scalare il residuo con l'odometro fino a una successiva lettura valida.

### 4. Verificare il ricalcolo

```gcode
BOX_SPOOL_GEOMETRY_STATUS ADDRESS=1 SLOT=1
BOX_SPOOL_GEOMETRY_STATUS ADDRESS=1 SLOT=3
```

Verificare UID, flags, `geometry_id`, revisione e lunghezza nominale. Per un
ricalcolo su misure conservate, la generazione deve restare invariata.
Il contatore `used_mm` non viene usato per dichiarare tutte le bobine nuove.

Nel collaudo con il nucleo da 83 mm:

| Riferimento | Misura grezza | Profilo / revisione | Percentuale CFS | Totale nominale |
|---|---:|---|---:|---:|
| Slot fisico 2, nuovo | 508,253 mm | 1 / 2 | 100% | 327 m |
| Slot fisico 4, usato | 302,823 mm | 1 / 2 | 12% | 327 m |

Entrambi conservarono generazione 1 e flags 0x13. La nuova sessione RH1 aveva
request 0: il caricamento della nuova geometria non avviò un'altra scansione.
Il residuo visualizzato dello slot 4 era 39,24 m, calcolato dal valore intero
12%. Con misure approssimate va considerato una stima.

## Correzione dopo un esaurimento reale

La misura dello slot fisico 4 era 302,823 mm. Con C_nucleo=260,8 e
C_pieno=508,3 il CFS assegnava il 12%. Al primo esaurimento il contatore
CFS ha registrato used_mm=19655 su total_mm=327000, mentre mostrava ancora
il 6%. Il cambio automatico allo slot 2 e riuscito. Questo conferma consumo
e cambio su R3, ma mostra che il primo coefficiente sovrastimava il residuo.

Per una misura iniziale conservata e un contatore che parte da quella misura:

```text
p_reale = lunghezza_consumata_fino_a_esaurimento / lunghezza_nominale
C_nucleo_effettivo = sqrt((C_misurata^2 - p_reale * C_pieno^2) / (1 - p_reale))

p_reale = 19655 / 327000 = 0,060107
C_nucleo_effettivo = 284,681 mm -> 284,7 mm
```

Con questi coefficienti la vecchia misura iniziale corrisponde al 6,005%,
mentre la bobina nuova resta al 100%. L'equivalente diametro di circa 90,62 mm
e un parametro effettivo che assorbe lo scarto del modello: non sostituisce
la misura fisica del cartone da 83 mm. La correzione e provvisoria, ricavata da
un solo esaurimento e da un contatore CFS; verificare su altre bobine della
stessa geometria e, se possibile, confrontare peso/consumo indipendente.

Annotare sempre misura iniziale, UID, generazione, total_mm e used_mm alla
partenza e al runout. Se il contatore non parte da zero, usare la differenza.
Non usare soltanto il 6% arrotondato, non modificare C_pieno per correggere
il limite inferiore e non avviare una scansione meccanica sulla bobina vuota
per cercare il nucleo: la misura CFS richiede filamento trascinabile.

Il file attivo sul riferimento e stato salvato con C_nucleo=284,7 durante la
stampa; nessun riavvio ne ha applicato la modifica in RAM durante quella
stampa. Alla successiva attivazione a CFS inattivo, verificare catalogo e
associazione. Se una vecchia misura ha gia consumo protetto, occorre una
nuova lettura valida prima che venga associata al nuovo profilo.

## Formula e continuita del residuo

```text
percentuale_iniziale = round(100 * (C_misurata^2 - C_nucleo^2)
                                / (C_pieno^2 - C_nucleo^2))
```

La quantizzazione CFS limita il valore a 1..100%: correggere la geometria
non introduce uno zero nel protocollo. Il runout fisico resta la prova di
esaurimento e Kalico puo azzerare la disponibilita dello slot rimosso.
Una nuova identita RFID non significa bobina piena. La lunghezza nominale
resta per UID; la cache e l'odometro mantengono la continuita dopo perdita RAM.

## Ambito della prova

R3 verificata sul riferimento: letture manuali RH1 nei due slot, UID coerenti,
geometria RAM e sostituzione catalogo senza nuova scansione quando used_mm=0,
consumo dello slot 4 e cambio automatico a slot 2. La correzione a 284,7 mm
e verificata offline e attende la prossima prova fisica. Inserimento RFID
automatico, scollegamento fisico e misura diretta dello stack restano separati.
Vedere [CFS single-cycle RFID](CFS_SINGLE_CYCLE_RFID.md).
