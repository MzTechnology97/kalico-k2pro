# Kalico CFG6 - Funzioni avanzate (italiano)

**Sviluppo sperimentale. Non installare il firmware CFG6 per stampe di produzione.** La CFS reale resta con CFG5T e l'extra attivo non viene sostituito.

Identità richiesta dalla controparte firmware: K2OH, revisione v323-cfg6, feature 0xE3, API v2, 28 parametri. Le scritture vengono autorizzate soltanto dopo l'esatta verifica di tutti questi campi. OpenCFS (OCFS), Creality originale, CFG4 e CFG5T non ricevono SET CFG6.

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

## Esempio (tutto commentato finché non viene qualificato)

    [box_cfs_runtime]
    auto_apply: False
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

[Guida inglese](CFS_RUNTIME_CFG6_EN.md) | [Sorgenti firmware CFG6](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/feat/cfg6-full-hooks-lab/firmware/v3.25-cfg6-lab)
