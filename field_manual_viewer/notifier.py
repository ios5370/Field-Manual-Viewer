"""Local interactive alert; the durable report remains available if UI is unavailable."""
import ctypes
import sys


def main():
    status,host,seconds=sys.argv[1:4]
    message=(f'{host} requested backoff (HTTP {status}).\n\n'
             'Field Manual Viewer paused the acquisition to respect the server.\n'
             f'Wait at least {seconds} seconds, then use Resume-Acquisition.bat.\n'
             'The next run will use the 1-second file-request interval.\n\n'
             'Details: Field Manual\\reports\\server-backoff.json')
    ctypes.windll.user32.MessageBoxW(None,message,'Field Manual Viewer acquisition paused',0x00000030|0x00040000)


if __name__=='__main__':main()
