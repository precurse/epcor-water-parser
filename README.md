# Epcor Water 
The daily output can be found at [https://github.com/precurse/epcor-water-results](https://github.com/precurse/epcor-water-results)

Some water data is only available in the [monthly reports](https://www.epcor.com/products-services/water/water-quality/Pages/water-quality-reports-edmonton.aspx#/waterworks_), which usually lag behind by a month or two. The [daily water report data](https://www.epcor.com/products-services/water/water-quality/Pages/daily-water-quality.aspx) is used as much as possible otherwise. This is the best we have (for now).

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
