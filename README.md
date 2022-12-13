# Epcor Water 


## Installation
```sh
pip3 install -r requirements.txt
```

## Example Usage (Help)
```sh
usage: epcor-water.py [-h] [--zone {ELS,Rossdale}]

Calculate water stats from EPCOR water reports

options:
  -h, --help            show this help message and exit
  --zone {ELS,Rossdale}, -z {ELS,Rossdale}
```

## Example Usage (ELSmith)
```sh
$ python3 epcor-water.py --zone ELS
Using data for october-2022
ph: 7.9
calcium: 42.0
magnesium: 13.5
sulphate: 54.5
chloride: 4.81
```

## Example Usage (Rossdale)
```sh
$ python3 epcor-water.py --zone Rossdale
Using data for october-2022
ph: 8.0
calcium: 42.0
magnesium: 13.5
sulphate: 54.5
chloride: 4.81
```
