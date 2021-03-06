"""
Defines command-line utilities for use with packet
"""

import sys

from secrets import token_hex
from datetime import datetime, time, timedelta
import csv
import click

from packet.mail import send_start_packet_mail
from packet.notifications import packet_starting_notification, packets_starting_notification
from . import app, db
from .models import Freshman, Packet, FreshSignature, UpperSignature, MiscSignature
from .ldap import ldap_get_eboard_role, ldap_get_active_rtps, ldap_get_3das, ldap_get_webmasters, \
    ldap_get_drink_admins, ldap_get_constitutional_maintainers, ldap_is_intromember, ldap_get_active_members, \
    ldap_is_on_coop


@app.cli.command('create-secret')
def create_secret():
    """
    Generates a securely random token. Useful for creating a value for use in the "SECRET_KEY" config setting.
    """
    print("Here's your random secure token:")
    print(token_hex())


packet_start_time = time(hour=19)
packet_end_time = time(hour=21)


class CSVFreshman:
    def __init__(self, row):
        self.name = row[0].strip()
        self.rit_username = row[3].strip()
        self.onfloor = row[1].strip() == 'TRUE'


def parse_csv(freshmen_csv):
    print('Parsing file...')
    try:
        with open(freshmen_csv, newline='') as freshmen_csv_file:
            return {freshman.rit_username: freshman for freshman in map(CSVFreshman, csv.reader(freshmen_csv_file))}
    except Exception as e:
        print('Failure while parsing CSV')
        raise e


def input_date(prompt):
    while True:
        try:
            date_str = input(prompt + ' (format: MM/DD/YYYY): ')
            return datetime.strptime(date_str, '%m/%d/%Y').date()
        except ValueError:
            pass


@app.cli.command('sync-freshmen')
@click.argument('freshmen_csv')
def sync_freshmen(freshmen_csv):
    """
    Updates the freshmen entries in the DB to match the given CSV.
    """
    freshmen_in_csv = parse_csv(freshmen_csv)

    print('Syncing contents with the DB...')
    freshmen_in_db = {freshman.rit_username: freshman for freshman in Freshman.query.all()}

    for csv_freshman in freshmen_in_csv.values():
        if csv_freshman.rit_username not in freshmen_in_db:
            # This is a new freshman so add them to the DB
            freshmen_in_db[csv_freshman.rit_username] = Freshman(rit_username=csv_freshman.rit_username,
                                                                 name=csv_freshman.name, onfloor=csv_freshman.onfloor)
            db.session.add(freshmen_in_db[csv_freshman.rit_username])
        else:
            # This freshman is already in the DB so just update them
            freshmen_in_db[csv_freshman.rit_username].onfloor = csv_freshman.onfloor
            freshmen_in_db[csv_freshman.rit_username].name = csv_freshman.name

    # Update all freshmen entries that represent people who are no longer freshmen
    for freshman in filter(lambda freshman: freshman.rit_username not in freshmen_in_csv, freshmen_in_db.values()):
        freshman.onfloor = False

    # Update the freshmen signatures of each open or future packet
    for packet in Packet.query.filter(Packet.end > datetime.now()).all():
        # Handle the freshmen that are no longer onfloor
        for fresh_sig in filter(lambda fresh_sig: not fresh_sig.freshman.onfloor, packet.fresh_signatures):
            FreshSignature.query.filter_by(packet_id=fresh_sig.packet_id,
                                           freshman_username=fresh_sig.freshman_username).delete()

        # Add any new onfloor freshmen
        # pylint: disable=cell-var-from-loop
        current_fresh_sigs = set(map(lambda fresh_sig: fresh_sig.freshman_username, packet.fresh_signatures))
        for csv_freshman in filter(lambda csv_freshman: csv_freshman.rit_username not in current_fresh_sigs and
                                                        csv_freshman.onfloor and
                                                        csv_freshman.rit_username != packet.freshman_username,
                                   freshmen_in_csv.values()):
            db.session.add(FreshSignature(packet=packet, freshman=freshmen_in_db[csv_freshman.rit_username]))

    db.session.commit()
    print('Done!')


@app.cli.command('create-packets')
@click.argument('freshmen_csv')
def create_packets(freshmen_csv):
    """
    Creates a new packet season for each of the freshmen in the given CSV.
    """
    print("WARNING: The 'sync-freshmen' command must be run first to ensure that the state of floor is up to date.")
    if input('Continue? (y/N): ').lower() != 'y':
        return

    # Collect the necessary data
    base_date = input_date('Input the first day of packet season')
    start = datetime.combine(base_date, packet_start_time)
    end = datetime.combine(base_date, packet_end_time) + timedelta(days=14)

    print('Fetching data from LDAP...')
    all_upper = list(filter(
        lambda member: not ldap_is_intromember(member) and not ldap_is_on_coop(member), ldap_get_active_members()))

    rtp = ldap_get_active_rtps()
    three_da = ldap_get_3das()
    webmaster = ldap_get_webmasters()
    c_m = ldap_get_constitutional_maintainers()
    drink = ldap_get_drink_admins()

    # Packet starting notifications
    packets_starting_notification(start)

    # Create the new packets and the signatures for each freshman in the given CSV
    freshmen_in_csv = parse_csv(freshmen_csv)
    print('Creating DB entries and sending emails...')
    for freshman in Freshman.query.filter(Freshman.rit_username.in_(freshmen_in_csv)).all():
        packet = Packet(freshman=freshman, start=start, end=end)
        db.session.add(packet)
        send_start_packet_mail(packet)
        packet_starting_notification(packet)

        for member in all_upper:
            sig = UpperSignature(packet=packet, member=member.uid)
            sig.eboard = ldap_get_eboard_role(member)
            sig.active_rtp = member.uid in rtp
            sig.three_da = member.uid in three_da
            sig.webmaster = member.uid in webmaster
            sig.c_m = member.uid in c_m
            sig.drink_admin = member.uid in drink
            db.session.add(sig)

        for onfloor_freshman in Freshman.query.filter_by(onfloor=True).filter(Freshman.rit_username !=
                                                                              freshman.rit_username).all():
            db.session.add(FreshSignature(packet=packet, freshman=onfloor_freshman))

    db.session.commit()
    print('Done!')


@app.cli.command('ldap-sync')
def ldap_sync():
    """
    Updates the upper and misc sigs in the DB to match ldap.
    """
    print('Fetching data from LDAP...')
    all_upper = {member.uid: member for member in filter(
        lambda member: not ldap_is_intromember(member) and not ldap_is_on_coop(member), ldap_get_active_members())}

    rtp = ldap_get_active_rtps()
    three_da = ldap_get_3das()
    webmaster = ldap_get_webmasters()
    c_m = ldap_get_constitutional_maintainers()
    drink = ldap_get_drink_admins()

    print('Applying updates to the DB...')
    for packet in Packet.query.filter(Packet.end > datetime.now()).all():
        # Update the role state of all UpperSignatures
        for sig in filter(lambda sig: sig.member in all_upper, packet.upper_signatures):
            sig.eboard = ldap_get_eboard_role(all_upper[sig.member])
            sig.active_rtp = sig.member in rtp
            sig.three_da = sig.member in three_da
            sig.webmaster = sig.member in webmaster
            sig.c_m = sig.member in c_m
            sig.drink_admin = sig.member in drink

        # Migrate UpperSignatures that are from accounts that are not active anymore
        for sig in filter(lambda sig: sig.member not in all_upper, packet.upper_signatures):
            UpperSignature.query.filter_by(packet_id=packet.id, member=sig.member).delete()
            if sig.signed:
                sig = MiscSignature(packet=packet, member=sig.member)
                db.session.add(sig)

        # Migrate MiscSignatures that are from accounts that are now active members
        for sig in filter(lambda sig: sig.member in all_upper, packet.misc_signatures):
            MiscSignature.query.filter_by(packet_id=packet.id, member=sig.member).delete()
            sig = UpperSignature(packet=packet, member=sig.member, signed=True)
            sig.eboard = ldap_get_eboard_role(all_upper[sig.member])
            sig.active_rtp = sig.member in rtp
            sig.three_da = sig.member in three_da
            sig.webmaster = sig.member in webmaster
            sig.c_m = sig.member in c_m
            sig.drink_admin = sig.member in drink
            db.session.add(sig)

        # Create UpperSignatures for any new active members
        # pylint: disable=cell-var-from-loop
        upper_sigs = set(map(lambda sig: sig.member, packet.upper_signatures))
        for member in filter(lambda member: member not in upper_sigs, all_upper):
            sig = UpperSignature(packet=packet, member=member)
            sig.eboard = ldap_get_eboard_role(all_upper[sig.member])
            sig.active_rtp = sig.member in rtp
            sig.three_da = sig.member in three_da
            sig.webmaster = sig.member in webmaster
            sig.c_m = sig.member in c_m
            sig.drink_admin = sig.member in drink
            db.session.add(sig)

    db.session.commit()
    print('Done!')


@app.cli.command('fetch-results')
@click.option('-f', '--file', 'file_path', required=False, type=click.Path(exists=False, writable=True),
        help='The file to write to. If no file provided, output is sent to stdout.')
@click.option('--csv/--no-csv', 'use_csv', required=False, default=False, help='Format output as comma separated list.')
@click.option('--date', 'date_str', required=False, default='', help='Packet end date in the format MM/DD/YYYY.')
def fetch_results(file_path, use_csv, date_str):
    """
    Fetches and prints the results from a given packet season.
    """
    end_date = None
    try:
        end_date = datetime.combine(datetime.strptime(date_str, '%m/%d/%Y').date(), packet_end_time)
    except ValueError:
        end_date = datetime.combine(input_date("Enter the last day of the packet season you'd like to retrieve results "
                                           'from'), packet_end_time)


    file_handle = open(file_path, 'w', newline='') if file_path else sys.stdout

    column_titles = ['Name (RIT Username)', 'Upperclassmen Score', 'Total Score', 'Upperclassmen', 'Freshmen',
            'Miscellaneous', 'Total Missed']
    data = list()
    for packet in Packet.query.filter_by(end=end_date).all():
        received = packet.signatures_received()
        required = packet.signatures_required()

        row = [
        '{} ({}):'.format(packet.freshman.name, packet.freshman.rit_username),
        '{:0.2f}%'.format(received.member_total / required.member_total * 100),
        '{:0.2f}%'.format(received.total / required.total * 100),
        '{}/{}'.format(received.upper, required.upper),
        '{}/{}'.format(received.fresh, required.fresh),
        '{}/{}'.format(received.misc, required.misc),
        required.total - received.total,
        ]
        data.append(row)

    if use_csv:
        writer = csv.writer(file_handle)
        writer.writerow(column_titles)
        writer.writerows(data)
    else:
        for row in data:
            file_handle.write(f'''

{row[0]}
\t{column_titles[1]}: {row[1]}
\t{column_titles[2]}: {row[2]}
\t{column_titles[3]}: {row[3]}
\t{column_titles[4]}: {row[4]}
\t{column_titles[5]}: {row[5]}

\t{column_titles[6]}: {row[6]}
''')


@app.cli.command('extend-packet')
@click.argument('packet_id')
def extend_packet(packet_id):
    """
    Extends the given packet by setting a new end date.
    """
    packet = Packet.by_id(packet_id)

    if not packet.is_open():
        print('Packet is already closed so it cannot be extended')
        return
    else:
        print('Ready to extend packet #{} for {}'.format(packet_id, packet.freshman_username))

    packet.end = datetime.combine(input_date('Enter the new end date for this packet'), packet_end_time)
    db.session.commit()

    print('Packet successfully extended')


def remove_sig(packet_id, username, is_member):
    packet = Packet.by_id(packet_id)

    if not packet.is_open():
        print('Packet is already closed so its signatures cannot be modified')
        return
    elif is_member:
        sig = UpperSignature.query.filter_by(packet_id=packet_id, member=username).first()
        if sig is not None:
            sig.signed = False
            db.session.commit()
            print('Successfully unsigned packet')
        else:
            result = MiscSignature.query.filter_by(packet_id=packet_id, member=username).delete()
            if result == 1:
                db.session.commit()
                print('Successfully unsigned packet')
            else:
                print('Failed to unsign packet; could not find signature')
    else:
        sig = FreshSignature.query.filter_by(packet_id=packet_id, freshman_username=username).first()
        if sig is not None:
            sig.signed = False
            db.session.commit()
            print('Successfully unsigned packet')
        else:
            print('Failed to unsign packet; {} is not an onfloor'.format(username))


@app.cli.command('remove-member-sig')
@click.argument('packet_id')
@click.argument('member')
def remove_member_sig(packet_id, member):
    """
    Removes the given member's signature from the given packet.
    :param member: The member's CSH username
    """
    remove_sig(packet_id, member, True)


@app.cli.command('remove-freshman-sig')
@click.argument('packet_id')
@click.argument('freshman')
def remove_freshman_sig(packet_id, freshman):
    """
    Removes the given freshman's signature from the given packet.
    :param freshman: The freshman's RIT username
    """
    remove_sig(packet_id, freshman, False)
