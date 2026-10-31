# Residuo CFS con precisione in millimetri

R4-PRECISION1 aggiunge una lettura passiva SGP1 al firmware CFS. R3 resta il
riferimento verificato su hardware; R4 e il codice host associato devono ancora
essere provati sulla stampante.

## Cosa cambia

Il CFS conserva gia la circonferenza grezza e il consumo in mm. Il percorso
precedente arrotonda prima la geometria a una percentuale intera; su 327 m,
un punto percentuale vale 3,27 m. SGP1 calcola invece:

```text
frazione = clamp((C_misurata - C_nucleo) * (C_misurata + C_nucleo)
                 / ((C_pieno - C_nucleo) * (C_pieno + C_nucleo)), 0, 1)
iniziale_mm = round(frazione * totale_mm)
residuo_mm = max(0, iniziale_mm - consumo_mm)
```

La forma fattorizzata evita la sottrazione di quadrati quasi uguali.
Si tratta di risoluzione numerica in mm: l'accuratezza fisica dipende ancora da
geometria, avvolgimento e misura. I coefficienti Bambu restano
`bambu = 1, 284.7, 508.3`, senza nuove costanti vendor nel firmware.

Esempio numerico con la misura 302,823 mm e totale 327000 mm:

| Geometria nucleo / pieno | Vecchio valore iniziale | SGP1 iniziale |
|---|---:|---:|
| 260,8 / 508,3 mm, riferimento storico | 12% = 39240 mm | 40688 mm |
| 284,7 / 508,3 mm, correzione al runout | 6% = 19620 mm | 19636 mm |

Le vecchie risposte percentuali e il loro minimo dell'1% restano compatibili.
Un residuo SGP1 di zero e una **stima matematica**, non una nuova prova di
bobina fisicamente vuota: non provoca movimenti, cambio bobina o runout.

## Quando il dato e utilizzabile

La lettura precisa richiede lo stesso UID autenticato, misura valida autenticata nello stesso ciclo,
generazione applicata uguale a quella conservata, geometria/revisione attive,
lunghezza nominale coerente e runtime legacy type4 valido. La percentuale
iniziale legacy deve corrispondere alla stessa geometria.

Un'altra scansione, un nuovo UID, catalogo cambiato, perdita RAM o runtime non
coerente possono rendere SGP1 non disponibile. In quel caso il valore preciso
non e zero: e **sconosciuto**, e Kalico conserva cache e consumo locale.
Non viene eseguita automaticamente una scansione per recuperarlo.

## Protocollo

Richiesta opcode `0x57`, payload `1D slot`, slot locale 0..3.
Il corpo standard ha lunghezza 5. Non modifica RAM persistente di lavoro,
contatori, catalogo, tag o EEPROM e non avvia RF o motori.

La risposta e di **72 byte**:

- byte 0..3: `SGP1`;
- byte 4..63: stesso contenuto dello snapshot SGD1, salvo byte7;
- byte7: 1 se la quantita precisa e utilizzabile, 0 se non disponibile;
- byte64..67: quantita iniziale in mm, uint32 little-endian;
- byte68..71: quantita rimanente in mm, uint32 little-endian.

Con byte7=0 entrambi gli ultimi campi sono zero e non sono quantita valide.
Tutti gli altri riservati, UID, qualita, totale, consumo e versione devono
essere validati. Il mittente ripristina la maschera IRQ prima della risposta.
La lettura e atomica rispetto agli interrupt configurabili, con i limiti
NMI/DMA gia documentati per le altre snapshot.

INFO SGF1 e i comandi precedenti rimangono invariati. Il nuovo host prova
questa sola lettura: uno status esplicito 1 seleziona il percorso legacy R3.
Timeout, dati malformati o una misura R4 non disponibile mantengono la cache,
senza ricostruire il dato da una percentuale arrotondata.

## Kalico e cache

L'extra box_spool_profiles autorizza il campione per UID, profilo, revisione e
totale della bobina. box_rfid_estimates usa i mm CFS e continua a sottrarre il
consumo locale tra i campioni. Le letture restano alla frequenza gia esistente.

Un campione ripetuto, o un contatore inferiore sulla stessa misura, non puo
riportare indietro il consumo locale. L'identita del campione viene salvata
con la stima e ripristinata al riavvio; i vecchi record di cache restano validi.
Il campo rfid_reported_percent mantiene il valore legacy, mentre rfid_percent,
rfid_estimated_percent e rfid_remaining_m derivano dalla quantita precisa.
L'arrotondamento pubblico a un decimale resta quello della PR104.

R3 e RDG2 continuano a funzionare con il percorso precedente. Non serve
aggiungere un parametro di configurazione per abilitare la precisione.

## Verifica a stampante inattiva

1. Installare i due moduli Kalico associati, box_spool_profiles.py e
   box_rfid_estimates.py, e riavviare Klipper soltanto a stampa conclusa.
2. Flashare il BIN R4-PRECISION1 verificando il suo manifest e SHA.
3. Riavviare Klipper dopo il flash per riconciliare catalogo e connessione.
   La cache resta il riferimento finche non esiste una misura valida.
4. Se la RAM del CFS e stata persa, eseguire una lettura RFID manuale valida
   con filamento scaricato, hub libero e CFS inattivo, come nella
   [guida di calibrazione](CFS_SPOOL_CALIBRATION.md).
5. Leggere senza muovere la bobina:

   ```gcode
   BOX_SPOOL_GEOMETRY_STATUS ADDRESS=1 SLOT=1 PRECISION=1
   ```

   SLOT=1 e lo slot fisico2. Verificare precise_valid=1, UID, generazione,
   revisione, total_mm, used_mm, initial_mm e remaining_mm.
6. Durante una prova di stampa verificare che used_mm aumenti, remaining_mm
   diminuisca e la cache resti coerente anche quando la percentuale intera
   non cambia. La query e passiva; la scansione meccanica resta riservata
   al CFS inattivo.

## Limiti della verifica

I test eseguono il vero codice ARM a indirizzo fisico, controllano campioni
numerici, dati invalidi, interruzioni simulate, contatori e compatibilita R3.
Non sostituiscono la prova fisica R4. Il firmware non estende il limite flash
preesistente e non aggiunge RAM riservata; il buffer delle risposte SGF1/RH1
cresce di 8 byte sullo stack del comando. Le funzioni meccaniche e i contatori
protetti restano quelli di R3.
