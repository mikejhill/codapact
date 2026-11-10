// Exercises the generated TypeScript GraphQL client: node smoke.ts <index.ts> <baseUrl>
const { Client } = await import(process.argv[2]);
const client = new Client({ baseUrl: process.argv[3], auth: { token: "secret" } });
const books = await client.bookTitles({ genre: "FICTION" });
const dash = await client.dashboard();
console.log(
  JSON.stringify({
    titles: books.map((b: { title: string }) => b.title),
    kinds: dash.results.map((r: { __typename: string }) => r.__typename),
  }),
);
