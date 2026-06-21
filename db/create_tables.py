import asyncio
import os
import sys
import asyncpg
from dotenv import load_dotenv

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

async def create_tables():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set!")
        sys.exit(1)
        
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if not os.path.exists(schema_path):
        print(f"schema.sql not found at {schema_path}!")
        sys.exit(1)
        
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()

    print("Connecting to Supabase using asyncpg...")
    try:
        conn = await asyncpg.connect(db_url, timeout=15)
        print("Executing SQL schema to drop and recreate tables...")
        # execute runs multiple statements separated by semicolons
        await conn.execute(sql)
        print("Tables created successfully!")
        await conn.close()
    except Exception as e:
        print("Error during table creation:", e)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(create_tables())
