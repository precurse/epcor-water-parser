import argparse
import calendar
import datetime
import logging
import requests
import sys
from decimal import *
from bs4 import BeautifulSoup
from io import BytesIO
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.converter import TextConverter
from pdfminer.layout import LAParams
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFSyntaxError

class WaterReport():
    def __init__(self):
        self.calcium_hardness = None
        self.total_hardness = None
        self.ph = None
        self.alkalinity = None
        self.sulphate = None
        self.chloride = None

    def get_calcium(self):
        return self.calcium_hardness * 0.4

    def get_magnesium(self):
        return (self.total_hardness - self.calcium_hardness)/4


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
        report.alkalinity = soup.find(id="AlkalinityLabel7").text
    except decimal.InvalidOperation:
        # Data invalid, fallback to previous day
        try:
            report.ph = Decimal(soup.find(id="phLabel6").text)
            report.alkalinity = soup.find(id="AlkalinityLabel6").text
        except:
            print("Error getting daily data")
            sys.exit(1)

def download_pdf(url):
  response = requests.get(url)
  return BytesIO(response.content)

def parse_pdf(pdf_file):
  rsrcmgr = PDFResourceManager()
  retstr = BytesIO()
  laparams = LAParams()
  device = TextConverter(rsrcmgr, retstr, laparams=laparams)
  interpreter = PDFPageInterpreter(rsrcmgr, device)

  for page in PDFPage.get_pages(pdf_file, pagenos=set(), maxpages=0, caching=True, check_extractable=True):
      interpreter.process_page(page)

  text = retstr.getvalue()
  retstr.close()
  device.close()
  return text.decode("utf-8")

def parse_lines_from_pdf(pdf_file):
  # parse the PDF and get all the text
  pdf_text = parse_pdf(pdf_file)

  # split the text by newline to get all the lines
  lines = pdf_text.split("\n")

  # return PDF lines
  return lines

def parse_values(pdf_lines, report):
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

    skip_count = 0

    for line in pdf_lines:
        # Ignore empty lines until we start ingesting values 
        # We need to include some empty values after we start monthly average values
        if line.strip() == "" and not monthly_average_start:
            continue

        # Loop until we get to first header
        if line.startswith('Alkalinity'):
            header_start = True

        if line.startswith('Bacteriological Data'):
            continue

        # Done with headers
        if line.lower().startswith('e. coli'):
            headers.append(line)
            header_done = True
            continue

        # We're into the headers
        if header_start and not header_done:
            headers.append(line)

        # Start units column
        if header_done and not units_start:
            if line.startswith('mg'):
                units_start = True

        # Units column is done
        if header_start and len(units) == len(headers):
            units_done = True

        # Add to units if not finished with them yet
        if units_start and not units_done:
            units.append(line)

        # Start monthly count column
        if units_done and not monthly_count_start:
            monthly_count_start = True

        if header_done and len(monthly_count) == len(headers):
            monthly_count_done = True

        if monthly_count_start and not monthly_count_done:
            monthly_count.append(line)

        # Start monthly average column
        if monthly_count_done and not monthly_average_start:
            monthly_average_start = True

        if header_done and len(monthly_average) == len(headers)-2:
            monthly_average_done = True

        if monthly_average_start and not monthly_average_done:
            monthly_average.append(line)

        if monthly_average_done and not ytd_median_start and line != "":
            ytd_median_start = True

        if ytd_median_start and not ytd_median_done:
            ytd_median.append(line)

    # TODO: Add flag to dump all data
    # print data
    # print(f"{len(headers)} vs {len(monthly_average)}")
    # for i in range(len(headers)-2):
    #     print("{}{}{}".format(headers[i].ljust(30), units[i].ljust(12), monthly_average[i].rjust(10) ))

    data = {'headers': headers,
            'units': units,
            'monthly_average': monthly_average,
            }

    return data

def update_report_from_pdf(data, report):
    for idx, v in enumerate(data['headers']):
        if v.lower() == 'hardness, total':
            report.total_hardness = int(data['monthly_average'][idx])

        if v.lower() == 'calcium hardness':
            report.calcium_hardness = int(data['monthly_average'][idx])

        if v.lower() == 'sulphate dissolved':
            report.sulphate = Decimal(data['monthly_average'][idx])

        if v.lower() == 'chloride dissolved':
            report.chloride = Decimal(data['monthly_average'][idx])

def get_previous_months(n):
    today = datetime.date.today()

    previous_month = today - datetime.timedelta(weeks=4*n)
    previous_month_name = previous_month.strftime("%B").lower()
    previous_month_year = previous_month.strftime("%Y")

    return f"{previous_month_name}-{previous_month_year}"

def main():
    parser = argparse.ArgumentParser(description='Calculate water stats from EPCOR water reports')
    parser.add_argument('--zone','-z', choices=['ELS', 'Rossdale'], default='ELS')
    args = parser.parse_args()

    report = WaterReport()
    download_daily_data(report, zone=args.zone)
    data = None

    # Try getting current month's and previous 2 months data
    for i in range(0,3):
        mon = get_previous_months(i)
        url = f"https://www.epcor.com/products-services/water/water-quality/wqreportsedmonton/wwq-edmonton-{mon}.pdf"
        try:
            pdf_file = download_pdf(url)
            pdf_lines = parse_lines_from_pdf(pdf_file)
            print(f"Using data for {mon}")
            data = parse_values(pdf_lines, report)
        except PDFSyntaxError as e:
            # Keep trying other months data
            logging.debug(f"Failed to get data for {mon}")
            continue
        else:
            break

    update_report_from_pdf(data, report)

    print(f"ph: {report.ph}")
    print(f"calcium: {report.get_calcium()}")
    print(f"magnesium: {report.get_magnesium()}")
    print(f"sulphate: {report.sulphate}")
    print(f"chloride: {report.chloride}")

if __name__ == "__main__":
    main()
