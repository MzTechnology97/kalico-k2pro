# Kalico CFG6 - Funzioni avanzate (italiano)

**Firmware CFG6 sperimentale attualmente installato e verificato in SET/GET sul CM5.** Le prove dinamiche di carico/scarico, RFID e stabilità prolungata sono ancora necessarie prima dell'uso in produzione.

Identità richiesta dalla controparte firmware: K2OH, revisione v323-cfg6, feature 0xE3, API v2, 28 parametri. Le scritture vengono autorizzate soltanto dopo l'esatta verifica di tutti questi campi. OpenCFS (OCFS), Creality originale, CFG4 e CFG5T non ricevono SET CFG6.

**[Documentazione completa di tutti i 28 parametri: funzione reale, range, valori CM5 e rischi](CFS_RUNTIME_CFG6_PARAMETERS_IT.md).**

## Parametri scrivibili

Nuovi ID: 6 (reverse de-tooth), 9 (pullback), 11/12/13 (velocità RFID), 14/15/16/17 (tempi RFID), 18/19 (bobina adiacente), 21 (recupero feed), 23 (tentativi RFID), 24 (timeout de-tooth), 27 (cicli pretensione).

Restano scrivibili anche gli ID precedenti 1/2/3/5 (velocità hub), 7/8 (tempi hub), 20 (timeout totale feed) e 22 (timeout odometro): **23 parametri in tutto**. Gli ID 0/4, 10/25/26 sono volutamente non modificabili, poiché il firmware stock non fornisce la corrispondente regolazione operativa.

## Note importanti

- ID 6: disabilitazione reversibile del ramo de-tooth tramite sidecar RAM separata. Non vengono scritti byte di stato stock non documentati.
- ID 17: ritardo stock autentico 400 ms; aggancio corretto al sito 0x08012A60, non al separato ritardo da 10 ms.
- ID 27: numero dei cicli di pretensione, mediante il confronto stock a 0x08012616.
- ID 8 deve essere multiplo di 10 ms; ID 20/22 multipli di 20 ms. Tutti i valori sono volatili RAM.
- Nessun comando SET, RESET o APPLY dalla console; soltanto configurazione dalla sezione box_cfs_runtime al riavvio, con auto_apply abilitato consapevolmente.
- Il residuo type 4 live della CFG5T rimane presente, con stima G-code di riserva; un test reale è ancora necessario.

## Esempio (su macchina non ancora verificata, lasciare disabilitato)

    [box_cfs_runtime]
    auto_apply: False # Attivare solo dopo avere confermato la firma CFG6 e il bus
    #reverse_detooth_enable: True
    #hub_pullback_timeout_ms: 5000
    #rfid_seek_speed: 160
    #rfid_measure_speed: 150
    #rfid_reposition_speed: 100
    #rfid_seek_move_ms: 800
    #rfid_seek_settle_ms: 200
    #rfid_post_auth_wait_ms: 1000
    #rfid_pre_measure_settle_ms: 400
    #rfid_neighbor_detect_delay_ms: 400
    #rfid_neighbor_settle_ms: 100
    #feed_recovery_timeout_ms: 3000
    #rfid_neighbor_retry_count: 1
    #reverse_detooth_timeout_ms: 1000
    #load_retry_count: 1

I test Kalico sono simulati e non inviano comandi alla macchina. Conservare CFG5T e v3.22 per il recupero.

[Guida inglese](CFS_RUNTIME_CFG6_EN.md) | [Sorgenti firmware CFG6](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/main/firmware/v3.25-cfg6-lab)

## Prima verifica fisica CFG6 — 18 ottobre 2026

La CFS G32/APP153, aggiornata manualmente, è stata identificata con `K2OH / v323-cfg6 / 0xE3`, API2 e 28 ID. Con stampante ferma (`standby`), CFS `IDLE` e senza assi referenziati, `auto_apply: True` è stato abilitato nel `box.cfg` del CM5. Al riavvio del solo Klipper l'extra ha riportato **`applied 23 value(s)`**, senza errori né opzioni ignorate. Una successiva diagnostica `BOX_CFS_CONFIG_DIAG ALL=1` ha letto **28/28 valori validi**, con tutti i 23 override corrispondenti ai GET del firmware.

Sono state confermate anche le personalizzazioni: `hub_transition_speed=220`, `hub_insert_speed=100`, `hub_reverse_speed=150`, `rfid_measure_speed=150`, `rfid_neighbor_retry_count=1` e `load_retry_count=1`. Gli ID 0, 4, 10, 25, 26 rimangono in sola lettura.

**Non sono stati eseguiti G28, homing Z, spostamenti né movimenti filamento.** Alla prima inizializzazione dopo il flash il bus aveva richiesto un secondo tentativo di enumerazione dei motori con errori timeout/CRC; al successivo riavvio con `auto_apply` i motori sono risultati pronti al primo tentativo, con un timeout broadcast. Pertanto questa prova conferma il protocollo e la persistenza temporanea in RAM, **non la stabilità RS-485 a lungo termine, il funzionamento dinamico RFID/carico/scarico o il residuo type 4 in una stampa reale**.

Il template rimane interamente commentato per impedire scritture accidentali sulle nuove installazioni. Su una CFS CFG6 già verificata si può abilitare nel file attivo:

```ini
[box_cfs_runtime]
auto_apply: True
# Inserire i 23 valori del template CFG6, lasciando esclusi 0/4/10/25/26.
```

Conservare CFG5T e v3.22 come rollback.


Approfondimento tecnico ID0–27: [riferimento completo IT](CFS_RUNTIME_CFG6_PARAMETERS_IT.md) e [EN](CFS_RUNTIME_CFG6_PARAMETERS_EN.md).


**Aggiornamento attività e audit delle PR:** [stato CFG6 e issue ancora aperte](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/docs/CFG6_STATUS_PR_TRIAGE_20261018_IT.md) · [EN](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/docs/CFG6_STATUS_PR_TRIAGE_20261018_EN.md).