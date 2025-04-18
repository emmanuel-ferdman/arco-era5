# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import configparser
import datetime
import glob
import json
import logging
import os
import re
import typing as t

from google.cloud import secretmanager

from .utils import ExecTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FILES_TO_UPDATE = {
    ExecTypes.ERA5.value: ["dve", "o3q", "qrqs", "tw", "pl", "sl", "lnsp", "zs", "sfc"],
    ExecTypes.ERA5T_DAILY.value: ["dve", "o3q", "qrqs", "tw", "pl", "sl"],
    ExecTypes.ERA5T_MONTHLY.value: ["lnsp", "zs", "sfc"]
}

API_KEY_PATTERN = re.compile(r"^API_KEY_\d+$")


class ConfigArgs(t.TypedDict):
    """A class representing the configuration arguments for the new_config_file
    function.

    Attributes:
        year_wise_date (bool): True if the configuration file contains 'year',
                                'month' and 'day', False otherwise.
        first_day (datetime.date): The first day of the third previous month.
        last_day (datetime.date): The last day of the third previous month.
        sl_year (str): The year of the third previous month in 'YYYY' format.
        sl_month (str): The month of the third previous month in 'MM' format.
    """
    year_wise_date: bool
    first_day: datetime.date
    last_day: datetime.date
    sl_year: str
    sl_month: str


class MonthDates(t.TypedDict):
    """A class representing the first and third previous month's dates.

    Attributes:
        first_day (datetime.date): The first day of the third previous month.
        last_day (datetime.date): The last day of the third previous month.
        sl_year (str): The year of the third previous month in 'YYYY' format.
        sl_month (str): The month of the third previous month in 'MM' format.
    """
    first_day: datetime.date
    last_day: datetime.date
    sl_year: str
    sl_month: str


def new_config_file(config_file: str, field_name: str, additional_content: str,
                    config_args: ConfigArgs, temp_path: str = None) -> None:
    """Modify the specified configuration file with new values.

    Parameters:
        config_file (str): The path to the configuration file to be modified.
        field_name (str): The name of the field to be updated with the new value.
        additional_content (str): The additional content to be added under the
                                    '[selection]' section.
        config_args (ConfigArgs): A dictionary containing the configuration arguments
                                    as key-value pairs.
    """

    # Unpack the values from config_args dictionary
    year_wise_date = config_args["year_wise_date"]
    first_day = config_args["first_day"]
    last_day = config_args["last_day"]
    sl_year = config_args["sl_year"]
    sl_month = config_args["sl_month"]

    config = configparser.ConfigParser(interpolation=None)
    config.read(config_file)

    if temp_path:
        target_path = config.get("parameters", "target_path")
        config.set("parameters", "target_path", target_path.replace("gs://gcp-public-data-arco-era5/raw", temp_path))

    if year_wise_date:
        config.set("selection", "year", sl_year)
        config.set("selection", "month", sl_month)
        config.set("selection", "day", "all")
    else:
        config.set("selection", field_name,
                   f"{first_day}/to/{last_day}")

    sections_list = additional_content.split("\n\n")
    for section in sections_list[:-1]:
        sections = section.split("\n")
        new_section_name = sections[0].strip()
        config.add_section(new_section_name)
        api_url_name, api_url_value = sections[1].split("=")
        config.set(new_section_name, api_url_name.strip(), api_url_value.strip())
        api_key_name, api_key_value = sections[2].split("=")
        config.set(new_section_name, api_key_name.strip(), api_key_value.strip())

    with open(config_file, "w") as file:
        config.write(file, space_around_delimiters=False)


def get_month_range(date: datetime.date) -> t.Tuple[datetime.date, datetime.date]:
    """Return the first and last date of the previous month based on the input date.

    Parameters:
        date (datetime.date): The input date.

    Returns:
        tuple: A tuple containing the first and last date of the month as
                datetime.date objects.
    """
    last_day = date.replace(day=1) - datetime.timedelta(days=1)
    first_day = last_day.replace(day=1)
    return first_day, last_day


def get_previous_month_dates(mode: str) -> MonthDates:
    """Return a dictionary containing the first and last date to process.

    Returns:
        dict: A dictionary containing the following key-value pairs:
            - 'first_day': The first day (datetime.date).
            - 'last_day': The last day (datetime.date).
            - 'sl_year': The year of the date in 'YYYY' format (str).
            - 'sl_month': The month of the date in 'MM' format (str).
    """

    today = datetime.date.today()
    if mode == ExecTypes.ERA5T_DAILY.value:
        # Get date before 6 days
        third_prev_month = today - datetime.timedelta(days=6)
        first_day = last_day = third_prev_month
    elif mode == ExecTypes.ERA5T_MONTHLY.value:
        # Get date range for previous month
        third_prev_month = today
        first_day, last_day = get_month_range(third_prev_month)
    else:
        # Calculate the correct previous third month considering months from 1 to 12
        third_prev_month = today - datetime.timedelta(days=2*366/12)
        first_day, last_day = get_month_range(third_prev_month)
    sl_year, sl_month = str(first_day)[:4], str(first_day)[5:7]

    return {
        'first_day': first_day,
        'last_day': last_day,
        'sl_year': sl_year,
        'sl_month': sl_month,
    }


def get_api_keys() -> t.List[str]:
    api_key_list = []
    for env_var in os.environ:
        if API_KEY_PATTERN.match(env_var):
            api_key_value = os.environ.get(env_var)
            api_key_list.append(api_key_value)
    return api_key_list


def generate_additional_content() -> str:
    """Generate additional_content including API KEYS."""
    api_key_list = get_api_keys()

    additional_content = ""
    for count, secret_key in enumerate(api_key_list):
        secret_key_value = get_secret(secret_key)
        additional_content += f'parameters.api{count}\n\
            api_url={secret_key_value["api_url"]}\napi_key={secret_key_value["api_key"]}\n\n'
    return additional_content


def update_config_file(directory: str, field_name: str,
                       mode: str, temp_path: str = None) -> None:
    """Update the configuration files in the specified directory.

    Parameters:
        directory (str): The path to the directory containing the configuration files.
        field_name (str): The name of the field to be updated with the new value.
        additional_content (str): The additional content to be added under the
                    '[selection]' section.
    """
    dates_data = get_previous_month_dates(mode)
    config_args = {
        "first_day": dates_data['first_day'],
        "last_day": dates_data['last_day'],
        "sl_year": dates_data['sl_year'],
        "sl_month": dates_data['sl_month'],
    }
    files_to_update = FILES_TO_UPDATE[mode]
    if mode == ExecTypes.ERA5.value:
        all_files = glob.glob(f"{directory}/*/*.cfg")
    else:
        all_files = glob.glob(f"{directory}/{mode}/*.cfg")
        directory = f"{directory}/{mode}"
    
    additional_content = generate_additional_content()

    for filename in all_files:
        config_args["year_wise_date"] = False
        if any(chunk in filename for chunk in files_to_update):
            if "lnsp" in filename or "zs" in filename or "sfc" in filename:
                config_args["year_wise_date"] = True
            # Pass the data as keyword arguments to the new_config_file function
            new_config_file(filename, field_name, additional_content,
                            config_args=config_args, temp_path=temp_path)


def get_secret(secret_key: str) -> dict:
    """Retrieve the secret value from the Google Cloud Secret Manager.

    Parameters:
        api_key (str): The name or identifier of the secret in the Google
                        Cloud Secret Manager.

    Returns:
        dict: A dictionary containing the retrieved secret data.
    """
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": secret_key})
    payload = response.payload.data.decode("UTF-8")
    secret_dict = json.loads(payload)
    return secret_dict


def remove_license_from_config_file(config_file_path: str, num_licenses: int) -> None:
    """Remove licenses from a configuration file.

    Args:
        config_file_path (str): The path to the configuration file from
        which licenses will be removed.
        num_licenses (int): The number of licenses to remove from the file.

    """
    config = configparser.ConfigParser()
    config.read(config_file_path)
    for license_number in range(num_licenses):
        section_name = f'parameters.api{license_number}'
        config.remove_section(section_name)
    with open(config_file_path, "w") as file:
        config.write(file, space_around_delimiters=False)


def remove_licenses_from_directory(directory_path: str) -> None:
    """Remove licenses from all configuration files in a directory.

    Args:
        directory_path (str): The path to the directory containing configuration files.
        num_licenses (int): The number of licenses to remove from each
        configuration file.

    """
    num_licenses = len(get_api_keys())
    for filename in os.listdir(directory_path):
        if filename.endswith(".cfg"):
            config_file_path = os.path.join(directory_path, filename)
            remove_license_from_config_file(config_file_path, num_licenses)
