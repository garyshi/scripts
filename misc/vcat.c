#include <stdio.h>

#define DOTSIZE (1024*1024)
#define DOTLINE 50

int main(int argc, char *argv[])
{
	FILE *fp;
	long long counter, dotted;
	unsigned char buffer[4096];
	int n;

	fp = fopen(argv[1], "rb");
	if (fp == NULL) {
		fprintf(stderr, "failed open file\n");
		return 1;
	}

	counter = dotted = 0;

	while (!feof(fp)) {
		n = fread(buffer, 1, sizeof buffer, fp);
		fwrite(buffer, n, 1, stdout);
		fflush(stdout);
		counter += n;
		while (counter / DOTSIZE > dotted) {
			if (dotted % DOTLINE == 0) {
				fprintf(stderr, "%s %08lld ", argv[1], dotted);
			}
			fprintf(stderr, ".");
			if (dotted % DOTLINE == DOTLINE - 1) {
				fprintf(stderr, "\n");
			}
			fflush(stderr);
			dotted ++;
		}
	}

	fclose(fp);

	return 0;
}
