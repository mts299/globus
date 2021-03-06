#!/usr/bin/env python
# coding: utf-8
"""@package synchronizer
Last modification 201706 by Kevin Krieger

This script is designed to log on to the University of Saskatchewan globus
SuperDARN mirror in order to check for and download new files for a specific pattern and data type 

IMPORTANT: Before this script is run, there are a set of instructions which must be followed:
***
1) Go to https://auth.globus.org/v2/web/developers
    a) Add a project named 'SuperDARN' or something similar
    b) Add an app to that project called 'sync_radar_data'
    c) Add three scopes to the app:
i) urn:globus:auth:scope:transfer.api.globus.org:all (Transfer files using Globus Transfer)
ii) openid (Know who you are in Globus.)
iii) email (Know your email address.)
    d) The redirect url is : https://auth.globus.org/v2/web/auth-code
    e) Click the "Native App" checkbox
    f) Click 'create app'
    g) Now you have a 'client ID' available to you, used in the script sort of as your user name.

2) Install pip if you don't have it: on OpenSuSe: sudo zypper in python-pip
2.1) Install the globus sdk for python: sudo pip2 install globus-sdk OR sudo pip install globus-sdk
3) Edit the script to include your client_id on line 'CLIENT_ID ='
and the uuid of your endpoint on line 'PERSONAL_UUID ='
(found at "Endpoints" link of globus.org when you're logged in,
click on the endpoint then you'll see uuid in the information that pops up)
4) Now make sure the script is runnable: chmod +x sync_radar_data_globus.py
5) Now run the script with some arguments, such as:
"./sync_radar_data_globus.py -y 2007 -m 01 -p 20070101*sas /path/to/endpoint/dir"
it will ask you to log into globus to authenticate, give you a token to paste into the cmd line,
then it will save a refresh token to a file on your computer to use for automatic login from now on.
***

The first time it is run, it will ask for a manual login
to globus, in order to get a refresh token. It will save the refresh token so that subsequent calls
to the script can be automated (example: via cron).

By default, this script will email you upon failure of the transfer, but not upon success.
To change this behaviour, you need to simply change the 'notify_on_succeeded=False, 
notify_on_failed=True' values to True or False in the sync_files_from_list method.

By default, there is a soft timeout of 30 seconds per file for the transfer. If this amount of time 
is exceeded, the script will return, but the transfer is likely still happening. 
If you have a faster or slower connection, you may consider changing this value - it will not affect
the transfer, only the amount of time the script waits until returning. 
To be sure about the status of your transfer, you can log into globus.org and view your transfers.

The script will check the arguments and if there are errors with the
arguments (for example, if the year is in the future, or the month is
not 1-12) then it will fail with an error message.
"""

import globus_sdk
import inspect
from datetime import datetime
from os.path import expanduser, isfile
import sys
import argparse

HOME = expanduser("~")
# The following is a path to a file that contains the globus transfer refresh tokens used
# for automatic authentication
TRANSFER_RT_FILENAME = HOME + "/.globus_transfer_rt"

# UUID of your endpoint, retrieve from endpoint information at: https://www.globus.org/app/endpoints
PERSONAL_UUID = ''
# Client ID retrieved from https://auth.globus.org/v2/web/developers
CLIENT_ID = ''


def month_year_iterator(start_month, start_year, end_month, end_year):
    """ Found on stackoverflow by user S.Lott.
    
    :param start_month: the month you wish to start your iterator on, integer
    :param start_year: the year you wish to start your iterator on, integer
    :param end_month: the month you wish to end your iterator on, integer
    :param end_year: the year you wish to end your iterator on, integer
    :returns: An iterator over a set of years and months
    """
    ym_start = 12 * start_year + start_month - 1
    ym_end = 12 * end_year + end_month - 1
    for ym in range(ym_start, ym_end):
        y, m = divmod(ym, 12)
        yield y, m + 1


class Synchronizer(object):
    """ This is the synchronizer class. It knows about globus and will
    synchronize a given local directory with the globus SuperDARN mirror given by it's UUID
    Note:documentation at http://globus-sdk-python.readthedocs.io/en/stable/ was used extensively"""

    def __init__(self, client_id, client_secret=None, transfer_rt=None):
        """ Initialize the Synchronizer class by getting dates/times, arguments, checking for errors
        and finally getting a transfer client and mirror UUID to use.

            :param client_id: Retrieved from https://auth.globus.oorg/v2/web/developers for you
            :param client_secret: Not normally used, but another way of authentication
            :param transfer_rt: Transfer refresh token, used for automating authentication
        """
        self.cur_year = datetime.now().year
        self.cur_month = datetime.now().month
        self.possible_data_types = ['raw', 'dat', 'fit', 'map', 'grid', 'summary']

        # CLIENT_ID and CLIENT_SECRET are retrieved from the "Manage Apps" section of
        # https://auth.globus.org/v2/web/developers for this app. transfer_rt is retrieved
        # by looking for it at the path given by global variable TRANSFER_RT_FILENAME.
        self.CLIENT_ID = client_id
        self.CLIENT_SECRET = client_secret
        self.TRANSFER_RT = transfer_rt
        self.transfer_rt_filename = TRANSFER_RT_FILENAME

        parser = argparse.ArgumentParser()
        parser.add_argument("-y", "--sync_year", help="Year you wish to sync data for",
                            type=int, default=self.cur_year)
        parser.add_argument("-m", "--sync_month", help="Month you wish to sync data for",
                            type=int, default=self.cur_month)
        parser.add_argument("-p", "--sync_pattern", help="Sync pattern, default: '*'", default='*')
        parser.add_argument("-t", "--data_type",
                            help="One of {} Default: 'raw'".format(self.possible_data_types),
                            default='raw')
        parser.add_argument("sync_local_dir", help="Path on endpoint to sync data to")
        args = parser.parse_args()
        self.sync_year = args.sync_year
        self.sync_month = "{:02d}".format(args.sync_month)
        self.sync_pattern = args.sync_pattern
        self.data_type = args.data_type
        self.sync_local_dir = args.sync_local_dir

        self.sanity_check()

        # Get a transfer client
        self.transfer_client = self.get_transfer_client()
        self.mirror_uuid = self.get_superdarn_mirror_uuid()

    def sanity_check(self):
        """ Check arguments: year, month, data type. Kills script if something is
        wrong. """
        if int(self.sync_year) == self.cur_year and int(self.sync_month) > self.cur_month:
            sys.exit("Year {} and month {} is in the future. Exiting".format(self.sync_year,
                                                                             self.sync_month))
        if int(self.sync_year) > self.cur_year:
            sys.exit("Sync year \"{}\" is in the future. Exiting".format(self.sync_year))
        if int(self.sync_month) < 1 or int(self.sync_month) > 12:
            sys.exit("Sync month \"{}\" invalid. Exiting".format(self.sync_month))
        if self.data_type not in self.possible_data_types:
            sys.exit("Data type can only be one of {}. Exiting".format(self.possible_data_types))
        # Note we cannot check the path here with typical os.is_path
        # since we may be running this script on a different machine than the destination endpoint,
        # therefore the script will fail with exception from globus if it doesn't exist

    def synchronize(self):
        """ Do synchronization of files from the globus SuperDARN mirror to the user's endpoint """
        # Step 1 - Download files listing from server.
        # Go through listing, remove lines that don't correspond to requested pattern
        try:
            # You absolutely need the '~' in front of the root of the path for the patterns to work.
            # This isn't obvious from documentation.
            listing_path = "~/chroot/sddata/{}/{}/{}/".format(self.data_type,
                                                              self.sync_year,
                                                              self.sync_month)
            # The listing pattern is handled by the python globus sdk
            listing_pattern = "name:~*{}*".format(self.sync_pattern)
            if 'raw' in self.data_type:
                listing_pattern = "name:~*{}*rawacf.bz2".format(self.sync_pattern)
            elif 'dat' in self.data_type:
                listing_pattern = "name:~*{}*dat.bz2".format(self.sync_pattern)
            elif 'fit' in self.data_type:
                listing_pattern = "name:~*{}*fitacf.gz".format(self.sync_pattern)
            elif 'map' in self.data_type:
                listing_pattern = "name:~*{}*map".format(self.sync_pattern)
            elif 'grid' in self.data_type:
                listing_pattern = "name:~*{}*grid".format(self.sync_pattern)
            elif 'summary' in self.data_type:
                pass
            else:
                pass
            listing = self.transfer_client.operation_ls(self.mirror_uuid, path=listing_path,
                                                        filter=listing_pattern)
            files_to_sync = [entry['name'] for entry in listing]
            # Max duration of transfer is set to 30 seconds per file here to give plenty of time.
            # A more proper way to do this would be to get the sizes of the files to transfer
            # and calculate approximate time from that, given a typical network speed. This works
            # fine 99% of the time though.
            task_max_duration_s = len(files_to_sync) * 30
            print("Transferring {} files with a soft timeout of {} s.".format(len(files_to_sync),
                                                                              task_max_duration_s))

            # Step 2 - Use the file listing from step 1 and globus sync level 3 (checksum) to sync
            # requested pattern files. Block on this but timeout after a certain amount of time.
            transfer_result = self.sync_files_from_list(files_to_sync)
            completed = self.transfer_client.task_wait(transfer_result["task_id"],
                                                       timeout=task_max_duration_s,
                                                       polling_interval=30)
            if not completed:
                print("Transfer didn't complete yet but may still be running. Please check "
                      "https://www.globus.org/app/activity if you want to check status of transfer")
            else:
                print("Transfer finished")

        except globus_sdk.GlobusAPIError as e:
            print("Globus API error\nError code: {}\nError message: {}".format(e.code, e.message))
        except globus_sdk.GlobusConnectionError:
            print("Globus Connection Error - error communicating with REST API server")
        except globus_sdk.GlobusTimeoutError:
            print("Globus Timeout error - REST request timed out.")
        except globus_sdk.NetworkError:
            print("Network error")

    def get_superdarn_mirror_uuid(self):
        """ Will search endpoints and retrieve the UUID of the SuperDARN mirror endpoint. 
        
        :returns: UUID of SuperDARN mirror endpoint """

        for ep in self.transfer_client.endpoint_search('SuperDARN mirror'):
            if 'kevin.krieger@usask.ca' in ep['contact_email'] and 'Official' in ep['description']:
                return ep['id']
        sys.exit("No endpoint found for SuperDARN mirror. Exiting")

    def get_refresh_token_authorizer(self):
        """ Attempts to get an authorizer object that uses refresh tokens (for
        automatic authentication). It requires a refresh token to work. 
        
        :returns: Globus SDK authorizer object """

        # Get client from globus sdk to act on
        client = globus_sdk.NativeAppAuthClient(self.CLIENT_ID)
        client.oauth2_start_flow(refresh_tokens=True)

        # Get authorizer that handles the refreshing of token
        return globus_sdk.RefreshTokenAuthorizer(self.TRANSFER_RT, client)

    def get_client_secret_authorizer(self):
        """ Attempts to get an authorizer object that uses a client secret for authentication.
        Not normally used. 
        
        :returns: Globus SDK authorizer object """
        client = globus_sdk.ConfidentialAppAuthClient(self.CLIENT_ID, self.CLIENT_SECRET)
        token_response = client.oauth2_client_credentials_tokens()

        # the useful values that you want at the end of this
        globus_transfer_data = token_response.by_resource_server['transfer.api.globus.org']
        globus_transfer_token = globus_transfer_data['access_token']

        return globus_sdk.AccessTokenAuthorizer(globus_transfer_token)

    def get_auth_with_login(self):
        """ Attempts to get an authorizer object that requires manual authentication,
        but will return a refresh token and save it to  a local file for future use. 
        :returns: Globus SDK authorizer object """

        client = globus_sdk.NativeAppAuthClient(self.CLIENT_ID)
        client.oauth2_start_flow(refresh_tokens=True)

        authorize_url = client.oauth2_get_authorize_url()
        print('Please go to this URL and login: {0}'.format(authorize_url))

        # Handle both python 3 and python 2
        if sys.version_info > (3, 0):
            auth_code = input('Please enter the code you get after login here: ').strip()
        else:
            auth_code = raw_input('Please enter the code you get after login here: ').strip()
        token_response = client.oauth2_exchange_code_for_tokens(auth_code)

        globus_transfer_data = token_response.by_resource_server['transfer.api.globus.org']
        globus_transfer_token = globus_transfer_data['access_token']
        # Native apps - transfer_rt are refresh tokens and are lifetime credentials,
        # so they should be kept secret
        # The consents for these credentials can be seen at https://auth.globus.org/v2/web/consents
        print("Here is your refresh token: {}. It has been written to the file {}".
              format(globus_transfer_data['refresh_token'], self.transfer_rt_filename))
        with open(self.transfer_rt_filename, 'w') as transfer_rt_file:
            transfer_rt_file.write(globus_transfer_data['refresh_token'])

        print("Note: refresh tokens are lifetime credentials, they should be kept secret. Consents "
              "for these credentials are managed at https://auth.globus.org/v2/web/consents")

        return globus_sdk.AccessTokenAuthorizer(globus_transfer_token)

    def get_transfer_client(self):
        """ Determines what type of authorizer to get in order to initialize the TransferClient
        object which is used for many Globus SDK tasks (such as transferring files). Depending upon 
        whether or not the user has a refresh token or a client secret, the refresh token 
        authorizer, client secret authorizer or manual authorizer will be used. 
        
        :returns: Globus SDK Transfer Client object """

        if self.TRANSFER_RT is not None:
            return globus_sdk.TransferClient(authorizer=self.get_refresh_token_authorizer())
        elif self.CLIENT_SECRET is not None:
            return globus_sdk.TransferClient(authorizer=self.get_client_secret_authorizer())
        else:
            return globus_sdk.TransferClient(authorizer=self.get_auth_with_login())

    def sync_files_from_list(self, files_list, source_uuid=None, dest_uuid=PERSONAL_UUID):
        """ Takes a list of files to synchronize as well as source and destination endpoint UUIDs. 
        It is hard coded to place the files in the correct YYYY/MM directories on the SuperDARN 
        globus mirror, the default source and destination UUIDs are fine for 99% of usage. 
        
        :param files_list: python list of file names to synchronize
        :param source_uuid: UUID of the source endpoint of the files
        :param dest_uuid: UUID of the destination endpoint for the files
        :returns: Globus SDK transfer result object """

        if source_uuid is None:
            source_uuid = self.mirror_uuid
        function_name = inspect.currentframe().f_code.co_name
        transfer_data = globus_sdk.TransferData(self.transfer_client, source_uuid, dest_uuid,
                                                label=function_name, sync_level="checksum",
                                                notify_on_succeeded=False,
                                                notify_on_failed=True)
        source_dir_prefix = "/chroot/sddata/{}/{}/{}/".format(self.data_type,
                                                              self.sync_year,
                                                              self.sync_month)
        dest_dir_prefix = self.sync_local_dir
        for data_file in files_list:
            transfer_data.add_item("{}/{}".format(source_dir_prefix, data_file),
                                   "{}/{}".format(dest_dir_prefix, data_file))
        transfer_result = self.transfer_client.submit_transfer(transfer_data)
        return transfer_result


if __name__ == '__main__':
    """ Open the transfer refresh token file if it exists and use it to initialize a Synchronizer 
    object. Then synchronize! """
    if isfile(TRANSFER_RT_FILENAME):
        with open(TRANSFER_RT_FILENAME) as f:
            sync = Synchronizer(CLIENT_ID, transfer_rt=f.readline())
    else:
        sync = Synchronizer(CLIENT_ID)

    sync.synchronize()
