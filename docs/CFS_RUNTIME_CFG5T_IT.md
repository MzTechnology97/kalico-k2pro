# Kalico — Integrazione sperimentale CFG5T (IT)

**Stato: PR in bozza, non qualificata in hardware.** Il firmware K2OH v3.24 CFG5T espone identità 0x7A K2OH / v323-cfg5t e API2 con feature 0xE5. Non è compatibile con il firmware originale Creality, con altri firmware modificati o con la famiglia OpenCFS OCFS.

Il modulo box_cfs_runtime applica parametri soltanto dal CFG all'avvio di Kalico. La console non espone comandi SET/RESET/APPLY. Con CFG5T sono operativi gli ID 1, 2, 3, 5, 7, 8, 20, 22. Gli ID 20 e 22 hanno unità in millisecondi reali e richiedono multipli di 20 ms; ID 8 richiede multipli di 10 ms.

Il riconoscimento della funzionalità live_remaining è riservato alla combinazione esatta di identità e feature CFG5T. Box può seguire la percentuale rilevata dal firmware e conservare la stima da G-code come fallback. OpenCFS viene rilevato separatamente e non riceve comandi CFG5T.

## Procedura consigliata

1. Con stampante inattiva, flashare la CFG5T sperimentale tramite Helper tenendo CFG4 e v3.22 come rollback.
2. Con il vecchio extra attivo e auto_apply disattivato, verificare RS-485, enumerazione stepper e identità K2OH v323-cfg5t.
3. Soltanto dopo il primo controllo, installare l'extra della PR, riavviare Kalico a stampante inattiva e verificare la diagnostica in sola lettura.
4. Abilitare auto_apply nel CFG soltanto dopo aver impostato consapevolmente i valori desiderati. Non cambiare i timeout durante una stampa.
5. Con una prova di stampa controllata, verificare che il valore residuo type 4 diminuisca effettivamente; i test ARM con odometro simulato non sostituiscono una prova fisica.

[Sorgenti e BIN CFG5T](https://github.com/MzTechnology97/k2-cfs-rfid-tools/tree/main/firmware/v3.24-cfg5t) | [English guide](CFS_RUNTIME_CFG5T_EN.md)
