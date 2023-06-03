import argparse
import datetime
import logging
import requests
import sys
from decimal import Decimal, ROUND_UP, InvalidOperation
from bs4 import BeautifulSoup
from io import BytesIO
from pdfminer.high_level import extract_text
from pdfminer.pdfparser import PDFSyntaxError

class WaterReport():
    def __init__(self):
        self.alkalinity = None
        self.calcium_hardness = None
        self.chloride = None
        self.ph = None
        self.sodium = None
        self.sulphate = None
        self.total_hardness = None

    @property
    def calcium(self):
        calcium = self.calcium_hardness * 0.4
        calcium = Decimal(calcium).quantize(Decimal("0.1"), rounding=ROUND_UP)
        return calcium

    @property
    def magnesium(self):
        return (self.total_hardness - self.calcium_hardness)/4

    @property
    def bicarbonate(self):
        return (self.alkalinity/50)*61

    def get_influxdb(self):
        # line protocol
        # measurement,tag=foo value=bar ts
        pass

def download_daily_data(report, zone):
    url = f"https://apps.epcor.ca/DailyWaterQuality/Default.aspx?zone={zone}"
    r = requests.get(url)
    
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, 'lxml')
    else:
        print(f"Unable to get Daily Water Data at {url}")
        sys.exit(1)

    try:
        report.ph = Decimal(soup.find(id="phLabel7").text)
        report.alkalinity = Decimal(soup.find(id="AlkalinityLabel7").text)
        date = soup.find(id="DateLabel7").text
        print(f"Daily data from: {date}")
    except InvalidOperation:
        # Data invalid, fallback to previous day
        try:
            report.ph = Decimal(soup.find(id="phLabel6").text)
            report.alkalinity = Decimal(soup.find(id="AlkalinityLabel6").text)
            date = soup.find(id="DateLabel6").text
            print(f"Daily data from: {date}")
        except:
            print("Error getting daily data")
            sys.exit(1)


def download_pdf(url):
  response = requests.get(url)
  return BytesIO(response.content)

def parse_pdf(pdf_file):
  text = extract_text(pdf_file)
  return text

def parse_lines_from_pdf(pdf_file):
  # parse the PDF and get all the text
  pdf_text = parse_pdf(pdf_file)

  # split the text by newline to get all the lines
  lines = pdf_text.split("\n")

  # return PDF lines
  return lines

def parse_parameter_names(pdf_lines):
    parameters = []
    parameter_name_start = False

    for line in pdf_lines:
        logging.debug(f"Parsing line {line} for parameter name")

        if line.startswith('Alkalinity'):
            logging.debug("Found Alkalinity. Starting parameter name parsing")
            parameter_name_start = True

        # Bacteriological Data is a heading, and not a parameter
        if parameter_name_start and line.startswith('Bacteriological Data'):
            logging.debug("Ignoring Bacteriological Data parameter")
            continue

        # e. coli is the last parameter name
        if line.lower().startswith('e. coli'):
            logging.debug("Found e.coli. Done with Parameter name parsing")
            parameters.append(line)
            return parameters

        if parameter_name_start and line.strip() != "":
            parameters.append(line)


def parse_units(pdf_lines, max_count):
    units = []
    units_start = False

    for line in pdf_lines:
        if line.startswith('mg'):
            units_start = True

        # Add to units if not finished with them yet
        if units_start and line.strip() != "":
            units.append(line)

        if len(units) == max_count:
            return units

def parse_monthly_counts(pdf_lines, max_counts):
    counts = []

    for line in pdf_lines:
        if len(counts) == max_counts:
            return counts

        # Monthly counts must always be integers
        try:
            counts.append(int(line))
        except ValueError:
            continue

def parse_monthly_averages(pdf_lines, max_counts):
    # Once we hit our first set of integers, skip that many
    # These are monthly counts

    averages = []

    start_integers = False
    skip_values = 0
    for line in pdf_lines:
        if not start_integers:
            try:
                int(line)
                start_integers = True
            except ValueError:
                continue

        if skip_values <= max_counts:
            skip_values += 1
            continue

        # Ignore any empty rows. Assuming "Alkalinity" row always has a value
        if len(averages) <= 0 and line.strip() == "":
            continue

        averages.append(line)

        # -2 to ignore Bacteriological Data
        if len(averages) == max_counts-2:
            return averages

def parse_values(pdf_lines, report, print_report=False):
    headers = []
    units = []
    monthly_count = []
    monthly_average = []
    ytd_median = []

    header_start = False
    header_done = False

    units_start = False
    units_done = False

    monthly_count_start = False
    monthly_count_done = False

    monthly_average_start = False
    monthly_average_done = False

    ytd_median_start = False
    ytd_median_done = False

    data = {}

    p = parse_parameter_names(pdf_lines)
    data['parameters'] = p
    # TODO: Fix. Remove "headers" name, since this is not an accurate name
    data['headers'] = p


    logging.debug(f"Got parameter names: {p}")

    u = parse_units(pdf_lines, len(p))
    data['units'] = u
    logging.debug(f"Got unit types: {u}")

    # Use unit_count length next, since it's more accurate
    mc = parse_monthly_counts(pdf_lines, len(u))
    logging.debug(f"Got monthly counts: {mc}")

    ma = parse_monthly_averages(pdf_lines, len(u))
    data['monthly_average'] = ma
    logging.debug(f"Got monthly averages: {ma}")

    if print_report:
        for i in range(len(p)-2):
            print("{}{}{}".format(p[i].ljust(30), u[i].ljust(12), ma[i].rjust(10) ))

    logging.debug(f"Parsed data: {data}")

    return data

def update_report_from_pdf(data, report):
    if data is None:
        print("Data is empty")
        sys.exit(1)

    try:
        data['headers']
    except KeyError:
        print("Invalid data passed: {}".format(data))

    for idx, v in enumerate(data['headers']):
        if 'hardness' in v.lower() and 'total' in v.lower():
            report.total_hardness = int(data['monthly_average'][idx])

        if v.lower() == 'calcium hardness':
            report.calcium_hardness = int(data['monthly_average'][idx])

        if v.lower() == 'sulphate dissolved':
            report.sulphate = Decimal(data['monthly_average'][idx])

        if v.lower() == 'chloride dissolved':
            report.chloride = Decimal(data['monthly_average'][idx])

        if v.lower() == 'sodium':
            report.sodium = Decimal(data['monthly_average'][idx])

def get_previous_months(n):
    today = datetime.date.today()

    previous_month = today - datetime.timedelta(weeks=4*n)
    previous_month_name = previous_month.strftime("%B").lower()
    previous_month_year = previous_month.strftime("%Y")

    return f"{previous_month_name}-{previous_month_year}"

def main():
    parser = argparse.ArgumentParser(description='Calculate water stats from EPCOR water reports')
    parser.add_argument('--zone','-z', choices=['ELS', 'Rossdale'], default='ELS')
    parser.add_argument('--full', action="store_true", default=False)
    parser.add_argument('--debug', action="store_true", default=False)
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    report = WaterReport()
    download_daily_data(report, zone=args.zone)
    data = None

    # Try getting current month's and previous 2 months data
    for i in range(1,4):
        mon = get_previous_months(i)
        url = f"https://www.epcor.com/products-services/water/water-quality/wqreportsedmonton/wwq-edmonton-{mon}.pdf"
        logging.debug(f"Trying url {url}")
        try:
            pdf_file = download_pdf(url)
            pdf_lines = parse_lines_from_pdf(pdf_file)
            print(f"Monthly data from: {mon}")
            data = parse_values(pdf_lines, report, print_report=args.full)
        except PDFSyntaxError as e:
            # Keep trying other months data
            logging.debug(f"Failed to get data for {mon}")
            continue
        else:
            break

    update_report_from_pdf(data, report)

    print(f"pH: {report.ph}")
    print(f"Calcium (Ca): {report.calcium}")
    print(f"Magnesium (Mg): {report.magnesium}")
    print(f"Sulphate (SO4): {report.sulphate}")
    print(f"Chloride (Cl): {report.chloride}")
    print(f"Sodium (Na): {report.sodium}")
    print(f"Bicarbonate (HCO3): {report.bicarbonate}")
    print(f"Alkalinity (CaCO3): {report.alkalinity}")

if __name__ == "__main__":
    main()
