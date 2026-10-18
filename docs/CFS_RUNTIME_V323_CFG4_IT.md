# Kalico — Runtime Config v3.23 CFG4 e OpenCFS

Il modulo Kalico rileva le CFS tramite 0x7A in sola lettura, dopo
l'enumerazione A1/A0. La famiglia K2OH con revisione v323-cfg4,
variante 4 e feature 0xD7 abilita solo i sei parametri realmente
operativi (ID 1, 2, 3, 5, 7, 8). OpenCFS è riconosciuto come famiglia
OCFS, con varianti RELEASE, INERT e BENCH, anche in assenza dell'API
Runtime Config 0x57. Non riceve mai comandi SET K2OH CFG4.

La scrittura CFG4 è compatibile **esclusivamente con il firmware
K2-OpenHost modificato distribuito da k2-cfs-rfid-tools**. La v3.22
condivide 0xD7 ma non l'identità v323-cfg4.

I valori si configurano solo nella sezione box_cfs_runtime e vengono
inviati all'avvio. SET, RESET e APPLY da console non sono disponibili.
Gli ID non operativi sono esplicitamente ignorati. I valori sono
volatili: si perdono al reset della CFS. ID8 usa millisecondi reali,
in multipli di 10.

Esempio:

    [box_cfs_runtime]
    auto_apply: true
    hub_transition_speed: 220
    hub_insert_speed: 100
    hub_reverse_speed: 150
    #insert_sensor_timeout_ms: 7000

[English guide](CFS_RUNTIME_V323_CFG4.md) |
[Parametri IT](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/docs/CFS_V323_PARAMETER_GUIDE_IT.md) |
[Parameters EN](https://github.com/MzTechnology97/k2-cfs-rfid-tools/blob/main/docs/CFS_V323_PARAMETER_GUIDE_EN.md)

La build CFG4 è ancora sperimentale e non qualificata per hardware reale.

