import fcntl
import os
import polars as pl
import duckdb
from typing import Dict, List
import time
import logging
from db_lock import acquire_lock, release_lock

# Define a global lock file path
LOCKFILE_PATH = "/tmp/duckdb_lock"


def load_and_join_data(db_filename: str, tables: List[str]) -> pl.DataFrame:
    """
    Reads specified tables from DuckDB and joins them into a single DataFrame.
    """
    try:
        # Read data with retry logic
        dataframes = read_db(db_filename, tables)
        logging.info("Successfully read data from DuckDB.")

        # Perform joins
        commitments_df = join_dataframes(dataframes)
        logging.info("Successfully joined DataFrames.")

        return commitments_df
    except Exception as e:
        logging.error(f"Error loading and joining data: {str(e)}")
        return pl.DataFrame()  # Return empty DataFrame on failure


def load_data_from_duckdb(db_filename: str) -> Dict[str, pl.DataFrame]:
    """
    Loads data from DuckDB tables and returns a dictionary of Polars DataFrames.
    """
    # Acquire lock before accessing DuckDB
    lockfile = acquire_lock(LOCKFILE_PATH)
    try:
        conn = duckdb.connect(db_filename)

        # Load data directly into Polars DataFrames
        encrypted_stores_df = conn.execute("SELECT * FROM encrypted_stores").pl()
        commit_stores_df = conn.execute("SELECT * FROM commit_stores").pl()
        commits_processed_df = conn.execute("SELECT * FROM commits_processed").pl()

        conn.close()

        # Store DataFrames in a dictionary
        dataframes = {
            "encrypted_stores": encrypted_stores_df,
            "commit_stores": commit_stores_df,
            "commits_processed": commits_processed_df,
        }

        return dataframes
    finally:
        # Release the lock after operation is done
        release_lock(lockfile)


def join_dataframes(dataframes: Dict[str, pl.DataFrame]) -> pl.DataFrame:
    """
    Performs joins on the provided DataFrames and returns the commitments DataFrame.
    """
    encrypted_stores_df = dataframes["encrypted_stores"]
    commit_stores_df = dataframes["commit_stores"]
    commits_processed_df = dataframes["commits_processed"]

    # Perform joins
    commitments_df = encrypted_stores_df.join(
        commit_stores_df, on="commitmentIndex", how="inner"
    ).join(
        commits_processed_df.select("commitmentIndex", "isSlash"),
        on="commitmentIndex",
        how="inner",
    )

    # Rename block number column for clarity
    commitments_df = commitments_df.with_columns(
        pl.col("block_number").alias("block_number_encrypted")
    )

    # Select desired columns
    commitments_df = commitments_df.select(
        "block_number_encrypted",  # from encrypted_stores
        "timestamp",
        "txnHash",
        "bid",
        "commiter",
        "bidder",
        "isSlash",
        "decayStartTimeStamp",
        "decayEndTimeStamp",
        "dispatchTimestamp",
        "commitmentHash",
        "commitmentIndex",
        "commitmentDigest",
        "commitmentSignature",
        "revertingTxHashes",
        "bidHash",
        "bidSignature",
        "sharedSecretKey",
    )

    return commitments_df


def get_latest_block_number(
    table_name: str, block_column: str, db_filename: str
) -> int:
    """
    Retrieves the latest (maximum) block number from the specified table and column.
    """
    # Acquire lock before accessing DuckDB
    lockfile = acquire_lock(LOCKFILE_PATH)
    try:
        conn = duckdb.connect(db_filename, read_only=True)

        # Check if the table exists
        table_exists = conn.execute(
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{table_name}'"
        ).fetchone()[0]

        if not table_exists:
            conn.close()
            return 0  # Table doesn't exist yet

        result = conn.execute(
            f"SELECT MAX({block_column}) FROM {table_name}"
        ).fetchone()[0]
        conn.close()
        return int(result) if result is not None else 0

    finally:
        # Release the lock after operation is done
        release_lock(lockfile)


def write_to_duckdb(df: pl.DataFrame, table_name: str, db_filename: str) -> str:
    """
    Writes a Polars DataFrame to a DuckDB table.
    Returns a string indicating the action taken for logging purposes.
    """
    if df.is_empty():
        # No new data to write
        return f"{table_name}: no new data"

    action = ""
    # Acquire lock before writing to DuckDB
    lockfile = acquire_lock(LOCKFILE_PATH)
    try:
        with duckdb.connect(db_filename) as conn:
            # Register the Polars DataFrame as a DuckDB view using Arrow
            conn.register("df_temp", df.to_arrow())

            # Check if the table exists
            table_exists = conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                [table_name],
            ).fetchone()[0]

            if not table_exists:
                # Create table and insert data
                conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM df_temp")
                action = f"{table_name}: created table with {len(df)} records"
            else:
                # Append data without checking for duplicates
                conn.execute(f"INSERT INTO {table_name} SELECT * FROM df_temp")
                action = f"{table_name}: inserted {len(df)} new records"

            # Unregister the temporary view
            conn.unregister("df_temp")
    except Exception as e:
        logging.error(f"Error writing to DuckDB table {table_name}: {e}")
        return f"{table_name}: Failed to write records."
    finally:
        # Release the lock after writing
        release_lock(lockfile)

    return action


def read_db(
    db_filename: str,
    tables: List[str],
    max_retries: int = 5,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
) -> Dict[str, pl.DataFrame]:
    """
    Reads specified tables from DuckDB into Polars DataFrames with retry logic for handling locks.

    Args:
        db_filename (str): Path to the DuckDB database file.
        tables (List[str]): List of table names to read.
        max_retries (int): Maximum number of retry attempts.
        initial_delay (float): Initial delay between retries in seconds.
        backoff_factor (float): Factor by which the delay increases after each retry.

    Returns:
        Dict[str, pl.DataFrame]: Dictionary of Polars DataFrames keyed by table names.

    Raises:
        Exception: If all retry attempts fail due to lock conflicts.
    """
    attempt = 0
    delay = initial_delay
    while attempt < max_retries:
        try:
            # Acquire lock before reading
            lockfile = acquire_lock(LOCKFILE_PATH)
            try:
                with duckdb.connect(db_filename, read_only=True) as conn:
                    dataframes = {}
                    for table in tables:
                        query = f"SELECT * FROM {table}"
                        df = conn.execute(query).pl()
                        dataframes[table] = df
                release_lock(lockfile)
                return dataframes  # Successful read
            except Exception as e:
                release_lock(lockfile)
                raise e
        except duckdb.IOException as e:
            if "Conflicting lock" in str(e) or "database is locked" in str(e).lower():
                attempt += 1
                logging.warning(
                    f"Lock detected on {db_filename}. Retrying in {delay} seconds... (Attempt {attempt}/{max_retries})"
                )
                time.sleep(delay)
                delay *= backoff_factor  # Exponential backoff
            else:
                logging.error(f"IO Error while reading DuckDB: {str(e)}")
                raise
    raise Exception(
        f"Failed to read from {db_filename} after {max_retries} attempts due to lock conflicts."
    )