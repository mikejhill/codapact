// Exercises the generated TypeScript petstore client: node smoke.ts <index.ts> <baseUrl>
const { Client, ApiError } = await import(process.argv[2]);
const client = new Client({ baseUrl: process.argv[3], auth: { token: "secret" }, maxRetries: 0 });
const pet = await client.pets.get({ petId: 3 });
const created = await client.pets.create({ name: "Ramen", status: "sold" });
const ids: number[] = [];
for await (const p of client.pets.listIter({ limit: 2 })) ids.push(p.id);
let status = 0;
try {
  await client.pets.get({ petId: 99 });
} catch (error) {
  if (error instanceof ApiError) status = error.status;
}
const health = await client.health();
console.log(JSON.stringify({ name: pet.name, created: created.status, ids, status, health }));
