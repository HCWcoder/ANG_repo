from argparse import ArgumentParser
from sys import argv


count = sum(map(argv.count, ["-l", "-p", "-f"]))
parser = ArgumentParser(description="Voting app for anghami")

parser.add_argument(
	"-l",
	"--like",
	type=str,
	required=count != 1,
	help="id of music to mass liking"
)

parser.add_argument(
	"-p",
	"--play",
	type=str,
	required=count != 1,
	help="id of music to mass playing"
)

parser.add_argument(
	"-f",
	"--follow",
	type=str,
	required=count != 1,
	help="id of artist to mass following"
)

parser.add_argument(
	"-v",
	"--votes",
	type=int,
	required=True,
	help="number of votes"
)

parser.add_argument(
	"-c",
	"--country",
	type=str,
	required=True,
	help="country of proxies [EG] or [RU] or [LB]"
)

parser.add_argument(
	"-t",
	"--threads",
	type=int,
	required=True,
	help="number of threads to use"
)

parser.add_argument(
	"--old_tokens",
	action="store_true",
	required=False,
	help="using old grecaptcha tokens"
)

args = parser.parse_args()