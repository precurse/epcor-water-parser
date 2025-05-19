import argparse
import datetime
import logging
import requests
import sys
import pdfplumber
from decimal import Decimal, ROUND_UP, InvalidOperation
from bs4 import BeautifulSoup
from io import BytesIO

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
        calcium = self.calcium_hardness * Decimal(0.4)
        calcium = Decimal(calcium).quantize(Decimal("0.1"), rounding=ROUND_UP)
        return calcium

    @property
    def magnesium(self):
        return (self.total_hardness - self.calcium_hardness)/Decimal(4.0)

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
  if response.status_code != 200:
      logging.debug(f"Epcor returned {response}")
      return None
  return BytesIO(response.content)

def parse_lines_from_pdf(pdf_filename):
  # parse the PDF and get all the text
  with pdfplumber.open(pdf_filename) as pdf:
    pdf_text = pdf.pages[0].extract_text()

  # split the text by newline to get all the lines
  lines = pdf_text.split("\n")

  # return PDF lines
  return lines

def parse_parameter_names(pdf_lines):
    parameters = []

    for line in pdf_lines:
        logging.debug(f"Parsing line {line} for parameter name")

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

def parse_values_from_pdf_lines(pdf_lines, report, print_report=False):
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

    # We only need to check for the following in the PDF:
    # Total Hardness
    # Calcium Hardness
    # Sulphate Dissolved
    # Chloride Dissolved
    # Sodium
    for line in pdf_lines:
        logging.debug(f"parse_values checking line: {line}")
        line_check = line.lower()

        # Get an array of integers to easily parse
        int_array = []
        for i in line_check.split():
            try:
                Decimal(i)
                int_array.append(i)
            except InvalidOperation:
                continue
        logging.debug(f"Metric values: {int_array}")
        if line_check.startswith('total hardness'):
            report.total_hardness = Decimal(int_array[1])
            logging.debug(f"Found total hardness of {val}")
        elif line_check.startswith('calcium hardness'):
            val = line_check.split()[3]
            report.calcium_hardness = Decimal(int_array[1])
            logging.debug(f"Found calcium hardness of {val}")
        elif line_check.startswith('sulphate dissolved'):
            val = line_check.split()[3]
            report.sulphate = Decimal(int_array[1])
            logging.debug(f"Found sulphate dissolved of {val}")
        elif line_check.startswith('chloride dissolved'):
            val = line_check.split()[3]
            report.chloride = Decimal(int_array[1])
            logging.debug(f"Found chloride dissolved of {val}")
        elif line_check.startswith('sodium'):
            val = line_check.split()[3]
            report.sodium = Decimal(int_array[1])
            logging.debug(f"Found sodium of {val}")

def update_report_from_yaml(report):
    import yaml
    """
    Reads a YAML file containing water data and returns it as a dictionary.

    :param file_path: Path to the YAML file.
    :return: Dictionary with water data.
    """
    try:
        with open("water_data.yml", 'r') as file:
            data = yaml.safe_load(file)
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return None
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        return None

    report.total_hardness = int(data['total_hardness'])
    report.calcium_hardness = int(data['calcium_hardness'])
    report.sulphate = int(data['sulphate'])
    report.chloride = data['chloride']
    report.sodium = data['sodium']
    

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

        if v.strip().lower() == 'calcium hardness':
            logging.debug("Found calcium hardness: {}".format(data['monthly_average'][idx]))
            report.calcium_hardness = int(data['monthly_average'][idx])

        if v.strip().lower() == 'sulphate dissolved':
            report.sulphate = Decimal(data['monthly_average'][idx])

        if v.strip().lower() == 'chloride dissolved':
            report.chloride = Decimal(data['monthly_average'][idx])

        if v.strip().lower() == 'sodium':
            report.sodium = Decimal(data['monthly_average'][idx])

def get_previous_months(n):
    today = datetime.date.today()

    previous_month = today - datetime.timedelta(weeks=4*n)
    previous_month_num = previous_month.strftime("%m")
    previous_month_year = previous_month.strftime("%Y")

    return f"{previous_month_year}-{previous_month_num}"

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
    get_pdf_data(report, args)

    #update_report_from_pdf(data, report)
    #update_report_from_yaml(report)

    print(f"pH: {report.ph}")
    print(f"Calcium (Ca): {report.calcium}")
    print(f"Magnesium (Mg): {report.magnesium}")
    print(f"Sodium (Na): {report.sodium}")
    print(f"Bicarbonate (HCO3): {report.bicarbonate}")
    print(f"Sulphate (SO4): {report.sulphate}")
    print(f"Chloride (Cl): {report.chloride}")
    print(f"Alkalinity (CaCO3): {report.alkalinity}")

def get_pdf_data(report, args):
    # Try getting current month's and previous 2 months data
    for i in range(1,5):
        year_mon = get_previous_months(i)
        url = f"https://www.epcor.com/content/dam/epcor/documents/water-quality-reports/{year_mon}_edmonton_water-quality_monthly-summary.pdf"
        logging.debug(f"Trying url {url}")
        pdf_file = download_pdf(url)
        if pdf_file is None:
            continue
        pdf_lines = parse_lines_from_pdf(pdf_file)
        print(f"Monthly data from: {year_mon}")
        parse_values_from_pdf_lines(pdf_lines, report, print_report=args.full)
        # We got a report, so break
        break

if __name__ == "__main__":
    main()
