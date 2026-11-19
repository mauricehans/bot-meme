# Funding Live — graphe SOL historique et direct

Version autonome, Python 3.10 ou plus recent. Elle ne remplace pas vos anciens
scripts : placez ces fichiers dans un sous-dossier, par exemple funding_live.
Aucun service externe n'est cree et aucune cle n'est incluse dans le paquet.

## Fichiers
- funding_live.py : menu, Helius HTTP/WebSocket, serveur local, reprise/export.
- funding_core.py : extraction, journal SQLite, graphe temporel et roles.
- interface.html : interface Canvas sans bibliotheque distante, legende et details.
- requirements.txt : dependance aiohttp 3.12 ou plus (moins de 4).
- env.exemple : modele de fichier .env.
- test_funding_live.py : tests locaux, aucun appel Helius reel.
- demonstration.html : photographie d'un exemple fictif hors ligne.

## Installation dans PowerShell
Depuis le dossier qui contient les fichiers :

    python -m pip install -r requirements.txt

Conserver/copier votre fichier .env prive dans ce MEME dossier, ou creer un .env
contenant :

    HELIUS_API_KEY=votre_cle

Ne pas partager .env. Le chargeur accepte CLE=valeur, commentaires en lignes
entieres et guillemets simples/doubles ; pas les extensions complexes de dotenv.
La variable d'environnement existante prime sur la valeur du fichier.

Tester d'abord l'interface sans cle et sans reseau Helius :

    python funding_live.py --demo

Puis ouvrir http://127.0.0.1:8765 dans un navigateur. La simulation ajoute des
mouvements dans le desordre puis reconstitue une chaine a deux tampons.
Les noms DEV_A, TAMPON_A, etc. sont fictifs. Ce n'est pas une collecte reelle.

Pour le suivi reel :

    python funding_live.py

Le serveur ecoute uniquement 127.0.0.1. Ne pas ouvrir interface.html directement :
il faut passer par l'adresse locale imprimee. En cas de port occupe :

    python funding_live.py --port 8766

Le programme n'ouvre pas automatiquement le navigateur. Le terminal doit rester
ouvert. Ctrl+C interrompt les taches et tente un dernier export.

## Menu
0. Creer une session, ou numero d'une session existante pour reprendre.
Mode 1 : historique sur une plage de slots inclusifs.
Mode 2 : direct avec flux Helius + rattrapage periodique HTTP.
Schema 1 : suivi sortant sans qualification DEV/TAMPON.
Schema 2 : DEV -> N TAMPONS -> DEV, motif repete.
N accepte 0,1,2,3 et plus ; c'est un nombre EXACT par execution, pas une plage.
N=0 : toutes les positions sont DEV ; N=2 : DEV,TAMPON 1,TAMPON 2,DEV...
L'adresse initiale est supposee DEV. DEV bleu #2563eb, TAMPON gris #9ca3af.
Aucune verification de creation de tokens et aucune conclusion de fraude.
Les annotations manuelles des anciennes versions ne sont pas lues.

La profondeur compte les transferts : N=2 necessite une profondeur 3 pour voir
le DEV suivant, 6 pour deux motifs complets. La valeur proposee est N+1.
Le seuil individuel est applique AVANT l'agregation des montants par paire.
Il n'y a pas de plafond total de wallets, transactions ou appels dans le graphe.
Le temps, la RAM, le disque et les limites du fournisseur restent applicables.

## Periode choisie et rattrapage
En direct, le premier slot propose est le slot FINALISE courant : Entree signifie
suivre depuis ce point, pas recuperer tout le passe. Un slot anterieur permet de
reconstruire une periode puis de continuer en direct. La saisie se fait en slots,
pas en heures/date. En historique, le slot 0 peut declencher une enorme collecte.

Chaque adresse nouvellement decouverte est ajoutee au filtre WebSocket (si elle
est sous la profondeur limite) ET son historique est interroge depuis le debut
de la periode choisie, pas depuis l'instant ou l'abonnement a ete cree.
Cela permet de retrouver une sortie survenue avant l'abonnement.
Les anciens mouvements de cette periode ne sont pas tous interpretes comme la
continuation du financement : le moteur applique la politique temporelle ci-dessous.

Apres interruption, choisir la meme session. SQLite conserve les mouvements,
les bornes de la page en cours, le jeton de pagination et le dernier slot balaye.
Le curseur n'est avance qu'apres ecriture des mouvements. Une page relue ne
cree pas de doublons (cle : signature + position d'instruction).
Un rattrapage interrompu reprend son ancienne borne haute avant de poursuivre.

Le rattrapage periodique couvre tous les wallets suivis, meme sans coupure du WS.
L'intervalle est demande au lancement (30 secondes par defaut) : c'est un temps
minimal ENTRE tours, pas une promesse de tout verifier chaque 30 secondes.
Une marge de 64 slots est relue entre deux fenetres pour reduire les trous lies
au retard d'indexation. Ce n'est PAS une garantie d'exhaustivite fournisseur.
Les erreurs et les fenetres non terminees restent visibles dans l'interface.
Reconnexion WS avec temporisation, pings toutes les 30 secondes et resouscription.
Ajout des adresses via nouvel abonnement, puis suppression de l'ancien apres accuse.

## Politique temporelle — limitation importante
Les evenements sont recus en desordre mais rejoues depuis le journal.
Pour suivre A -> X -> B, le slot de X -> B doit etre STRICTEMENT SUPERIEUR au
slot auquel X est devenu atteignable depuis A (dans le nombre d'etapes considere).
Un transfert sortant anterieur au financement n'est pas propage.

DEUX ETAPES DANS LE MEME SLOT NE SONT PAS RESOLUES PAR CETTE VERSION,
meme si un transactionIndex est disponible. Elles sont conservees dans
meme_slot_non_resolu.json et visibles dans le compte d'alertes, mais ne sont
pas utilisees pour decouvrir/surveiller la branche suivante. Les enchainer
correctement demande un traitement d'ordre intra-bloc/intra-transaction plus avance.
Un mouvement ambigu peut devenir admissible si un parcours plus ancien est trouve.
Ainsi cette version peut manquer une chaine rapide entierement dans le meme slot.
Elle ne doit pas etre presentee comme un detecteur exhaustif de financement.

Le role principal depend de la longueur MINIMALE d'un parcours temporel admissible.
Des parcours de longueurs differentes peuvent donner DEV et TAMPON : fond du role
principal, bordure orange et CONFLIT. Des numeros tampon differents seuls ne sont
pas un conflit DEV/TAMPON. Les cycles chronologiquement admissibles sont bornes
par la profondeur choisie. La chronologie ne prouve pas l'identite des fonds.

## Interface et exports
Le navigateur consulte seulement le serveur LOCAL chaque seconde. Le flux Helius
reste WebSocket ; la verification HTTP sert a la fiabilisation et au rattrapage.
Interface : zoom, deplacement, repositionnement des noeuds, clic pour les details,
compteurs, etat WebSocket, nombre d'adresses abonnees et etat du rattrapage.
Les boutons JSON/CSV exportent la photographie courante.
Un export sur disque est refait toutes les 5 secondes et a l'arret normal :

sessions_funding/<session>/
- suivi.sqlite (+ fichiers SQLite -wal/-shm eventuels) : journal et curseurs.
- configuration.json : parametres, sans cle API.
- graphe.json : graphe temporel retenu, etats et alertes.
- transferts.csv : mouvements retenus dans ce graphe.
- mouvements_collectes.csv : TOUS les mouvements stockes au-dessus du seuil,
  y compris ceux non retenus par la politique temporelle.
- meme_slot_non_resolu.json : sorties dans le meme slot qu'une arrivee admissible.
- graphe.html : photographie autonome, consultable hors ligne, PAS un suivi actif.

Pour copier/sauvegarder une session, arreter proprement le programme puis copier
son dossier entier. Ne pas ouvrir deux processus sur la meme session. Ne pas
supprimer le journal pour reprendre : les curseurs seuls ne suffisent pas.
Les vieux graphe.json/cache des autres versions ne sont pas importes.

## Acces Helius, couts et limites
transactionSubscribe est une extension Helius avec droits d'abonnement specifiques.
Verifier dans le dashboard que la cle peut appeler transactionSubscribe ET
getTransactionsForAddress. Les methodes et le streaming peuvent consommer des credits.
La surveillance est ici FINALIZED, en HTTP comme en WS : pas le mode le plus precoce,
mais pas de gestion de rollback/reorganisation implementee. La latence est variable.
En cas de refus WebSocket, l'interface le montre ; le rattrapage HTTP continue si
ses droits sont disponibles, ce qui N'EST PAS equivalent a un flux a faible latence.

Une seule liste accountInclude par abonnement ; au-dela de 50000 wallets suivis,
le partitionnement n'est pas implemente. L'erreur est visible et l'HTTP continue.
Pas de garantie de fluidite pour un grand graphe : reconstruction en memoire et
exports periodiques. Pas de VPS/serveur de production ni de supervision externe.

Le perimetre est SOL NATIF : instructions System transfer/transferWithSeed explicites,
instructions internes comprises. Pas tous les changements de solde, ni SPL/wSOL,
creations/fermetures de comptes ou swaps analyses comme tels.
Le script n'achete rien, ne vend rien et n'envoie aucune transaction blockchain.

## Verification locale

    python -m unittest -v test_funding_live.py

10 tests : motifs 0/1/2/3, couleurs, ordre des evenements, mouvements avant financement,
meme-slot non propage, conflits/profondeur, deduplication, persistance, extraction,
pagination/reprise, HTTP local reel et WebSocket avec transport simule.
Tests executes sur Python 3.12 ; syntaxe verifiee pour Python 3.10.
Pas de test avec votre cle Helius, votre compte ou votre navigateur.

## Documentation Helius utilisee
https://www.helius.dev/docs/api-reference/rpc/websocket/transactionsubscribe
https://www.helius.dev/docs/rpc/websocket
https://www.helius.dev/docs/api-reference/rpc/http/gettransactionsforaddress
