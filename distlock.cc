//MIT License

//Copyright (c) 2021 Daeyun Shin

//Permission is hereby granted, free of charge, to any person obtaining a copy
//of this software and associated documentation files (the "Software"), to deal
//in the Software without restriction, including without limitation the rights
//to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//copies of the Software, and to permit persons to whom the Software is
//furnished to do so, subject to the following conditions:

//The above copyright notice and this permission notice shall be included in all
//copies or substantial portions of the Software.

//THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//SOFTWARE.

#include <exception>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>

#include <google/protobuf/text_format.h>
#include <google/protobuf/util/time_util.h>
#include <grpcpp/grpcpp.h>
#include <leveldb/cache.h>
#include <leveldb/db.h>
#include <leveldb/filter_policy.h>
#include <leveldb/slice.h>
#include <re2/re2.h>

#include "cxxopts.hpp"
#include "generated/proto/distlock.grpc.pb.h"
#include "generated/proto/distlock.pb.h"
#include "gsl/assert"
#include "spdlog/sinks/stdout_color_sinks.h"
#include "spdlog/spdlog.h"

#define LOGGER spdlog::get("console")

using google::protobuf::Timestamp;
using grpc::Server;
using grpc::ServerBuilder;
using grpc::ServerContext;
using grpc::Status;
using std::string;

std::recursive_mutex db_write_mutex;
std::recursive_mutex db_delete_mutex;

// https://developers.google.com/protocol-buffers/docs/reference/google.protobuf#timestamp
Timestamp GetCurrentTime() {
  struct timeval tv {};
  gettimeofday(&tv, nullptr);
  Timestamp timestamp;
  timestamp.set_seconds(tv.tv_sec);
  timestamp.set_nanos(tv.tv_usec * 1000);
  return timestamp;
}

bool IsLockExpired(const distlock::Lock &lock, const Timestamp &current_time) {
  using google::protobuf::util::TimeUtil;
  if (not lock.has_expires_in() or
      TimeUtil::DurationToNanoseconds(lock.expires_in()) == 0) {
    return false;  // Zero means no expiration.
  }
  return lock.acquired_since() + lock.expires_in() <= current_time;
}

class LockManagerServiceImpl final
    : public distlock::LockManagerService::Service {
 public:
  Status AcquireLock(ServerContext *context,
                     const distlock::AcquireLockRequest *request,
                     distlock::AcquireLockResponse *response) override {
    return IsOK([&] { AcquireLock(request, response); });
  }

  Status AcquireMany(ServerContext *context,
                     const distlock::AcquireManyRequest *request,
                     distlock::AcquireManyResponse *response) override {
    return IsOK([&] { AcquireMany(request, response); });
  }

  Status ReleaseLock(ServerContext *context,
                     const distlock::ReleaseLockRequest *request,
                     distlock::ReleaseLockResponse *response) override {
    return IsOK([&] { ReleaseLock(request, response); });
  }

  Status ProcessBatch(ServerContext *context,
                      const distlock::ProcessBatchRequest *request,
                      distlock::ProcessBatchResponse *response) override {
    return IsOK([&] { ProcessBatch(request, response); });
  }

  Status ListLocks(ServerContext *context,
                   const distlock::ListLocksRequest *request,
                   distlock::ListLocksResponse *response) override {
    return IsOK([&] { ListLocks(request, response); });
  }

  Status CountLocks(ServerContext *context,
                   const distlock::CountLocksRequest *request,
                   distlock::CountLocksResponse *response) override {
    return IsOK([&] { CountLocks(request, response); });
  }

  Status GetCurrentServerTime(
      ServerContext *context,
      const distlock::GetCurrentServerTimeRequest *request,
      distlock::GetCurrentServerTimeResponse *response) override {
    return IsOK([&] { GetCurrentServerTime(request, response); });
  }

  leveldb::DB *get_db() const {
    Expects(db_);
    return db_;
  }

  void set_db(leveldb::DB *db) { db_ = db; }

 protected:
  // Wrappers around DB operations.
  leveldb::Status LookupKey(const leveldb::Slice &key, string *value) const {
    if (key.empty()) {
      throw std::runtime_error("Key should not be empty.");
    }
    if (key.size() > 1024) {
      throw std::runtime_error("Key is too long.");
    }
    return get_db()->Get(leveldb::ReadOptions(), key, value);
  }

  leveldb::Status PutKey(const leveldb::Slice &key,
                         const leveldb::Slice &value) {
    // Throws on assertion failure.
    Expects(!key.empty());
    Expects(key.size() <= 1024);
    Expects(!value.empty());
    Expects(value.size() <= 4096);
    return get_db()->Put(leveldb::WriteOptions(), key, value);
  }

  leveldb::Status DeleteKey(const leveldb::Slice &key) {
    Expects(!key.empty());
    Expects(key.size() <= 1024);
    return get_db()->Delete(leveldb::WriteOptions(), key);
  }

  Status IsOK(const std::function<void(void)> &callable) {
    try {
      callable();
    } catch (const std::exception &e) {
      LOGGER->error("Exception: {}", e.what());
      return Status(grpc::StatusCode::INVALID_ARGUMENT, "");
    } catch (...) {
      LOGGER->error("Unidentified Exception");
      return Status(grpc::StatusCode::UNKNOWN, "");
    }
    return Status::OK;  // OK if no exception.
  }

  // Throws std::runtime_error.
  void AcquireLock(const distlock::AcquireLockRequest *request,
                   distlock::AcquireLockResponse *response) {
    // Used as the reference time point.
    const auto current_time = GetCurrentTime();

    auto new_lock = request->lock();  // Will be modified.
    if (not new_lock.has_expires_in()) {
      LOGGER->error("expires_in is a required field.");
      throw std::runtime_error("expires_in is a required field.");
    }
    if (response->has_acquired_lock()) {
      LOGGER->error("acquired_lock must be empty.");
      throw std::runtime_error("acquired_lock must be empty.");
    }
    // A unique name for the distributed lock. This is the key in the database.
    const string &global_id = new_lock.global_id();

    string value_str;
    {  // Critical section. Keep it minimal. Get() and Put() must be
       // synchronized.
      const std::lock_guard<std::recursive_mutex> lock_guard(db_write_mutex);
      leveldb::Status lookup_status = LookupKey(global_id, &value_str);

      bool can_acquire = false;
      if (lookup_status.ok()) {
        // Overwrite if expired.
        distlock::Lock existing_lock;
        if (not existing_lock.ParseFromString(value_str)) {
          LOGGER->error(
              "Could not parse stored lock. Key: {}  First 200 characters: {}",
              global_id, value_str.substr(0, 200));
          throw std::runtime_error("Could not parse from serialized string");
        }
        // Caller can figure out why the request failed if it did.
        *response->mutable_existing_lock() = existing_lock;

        const bool is_expired = IsLockExpired(existing_lock, current_time);
        if (request->overwrite() or is_expired) {
          // Case 1: Overwrite existing lock instance.
          can_acquire = true;
        } else {
          // Case 2: Not expired. acquired_lock must be empty. Non-blocking.
        }

      } else if (lookup_status.IsNotFound()) {
        // Case 3: Insert a new lock instance. No existing_lock in this case.
        can_acquire = true;
      } else {
        // Case 4: Database error. Not sure if this happens. Get() failed but
        // not because the key doesn't exist.
        LOGGER->error("Database error. Key: \"{}\"", global_id);
        throw std::runtime_error("Database error.");
      }

      if (can_acquire) {
        // Set only if successful.
        *new_lock.mutable_acquired_since() = current_time;

        string serialized_new_lock;
        if (not new_lock.SerializeToString(&serialized_new_lock)) {
          LOGGER->error("Could not serialize lock instance.");
          throw std::runtime_error("Could not serialize lock instance.");
        }
        PutKey(global_id, serialized_new_lock);
        // This is how the caller knows if the request was successful.
        *response->mutable_acquired_lock() = new_lock;
      }
    }  // End of critical section.

    *response->mutable_elapsed() = GetCurrentTime() - current_time;
  }

  // Throws std::runtime_error.
  void AcquireMany(const distlock::AcquireManyRequest *request,
                   distlock::AcquireManyResponse *response) {
    size_t num_acquired = 0;
    for (const auto &req_i : request->requests()) {
      if (request->max_acquired_locks() > 0 and
          num_acquired >= request->max_acquired_locks()) {
        break;
      }

      auto *res_i_ptr = response->mutable_responses()->Add();
      AcquireLock(&req_i, res_i_ptr);

      bool is_successful = res_i_ptr->has_acquired_lock() and
                           res_i_ptr->acquired_lock().has_acquired_since();
      if (is_successful) {
        ++num_acquired;
      }
    }
  }

  // Non-existing or expired locks are OK. Caller can look at `released_lock`
  // and figure it out if needed.
  void ReleaseLock(const distlock::ReleaseLockRequest *request,
                   distlock::ReleaseLockResponse *response) {
    const auto start_time = GetCurrentTime();
    if (request->return_released_lock()) {
      string value_str;
      {  // Critical section. Not the same mutex as AcquireLock().
        const std::lock_guard<std::recursive_mutex> lock_guard(db_delete_mutex);
        if (LookupKey(request->lock().global_id(), &value_str).ok()) {
          DeleteKey(request->lock().global_id());
        }
      }
      if (!value_str.empty()) {
        response->mutable_released_lock()->ParseFromString(value_str);
      }
    } else {
      DeleteKey(request->lock().global_id());
    }
    *response->mutable_elapsed() = GetCurrentTime() - start_time;
  }

  // Throws std::runtime_error.
  void ProcessBatch(const distlock::ProcessBatchRequest *request,
                    distlock::ProcessBatchResponse *response) {
    const auto start_time = GetCurrentTime();

    using std::recursive_mutex;
    using std::unique_lock;
    // Conditional RAII locks.
    // TODO(daeyun): It is not always necessary to lock both.
    const auto db_write_mutex_guard =
        request->is_synchronized()
            ? unique_lock<recursive_mutex>(db_write_mutex)
            : unique_lock<recursive_mutex>();
    const auto db_delete_mutex_guard =
        request->is_synchronized()
            ? unique_lock<recursive_mutex>(db_delete_mutex)
            : unique_lock<recursive_mutex>();

    for (const auto &req_i : request->requests()) {
      auto *res_i_ptr = response->mutable_responses()->Add();
      using distlock::AnyRequest;
      switch (req_i.request_case()) {
        case AnyRequest::kAcquireLockRequest:
          AcquireLock(&req_i.acquire_lock_request(),
                      res_i_ptr->mutable_acquire_lock_response());
          break;
        case AnyRequest::kAcquireManyRequest:
          AcquireMany(&req_i.acquire_many_request(),
                      res_i_ptr->mutable_acquire_many_response());
          break;
        case AnyRequest::kReleaseLockRequest:
          ReleaseLock(&req_i.release_lock_request(),
                      res_i_ptr->mutable_release_lock_response());
          break;
        case AnyRequest::kGetCurrentServerTimeRequest:
          GetCurrentServerTime(
              &req_i.get_current_server_time_request(),
              res_i_ptr->mutable_get_current_server_time_response());
          break;
        case AnyRequest::kListLocksRequest:
          ListLocks(&req_i.list_locks_request(),
                    res_i_ptr->mutable_list_locks_response());
          break;
        default:
          LOGGER->error("Unrecognized request.");
          throw std::runtime_error("Unrecognized request.");
      }
    }

    *response->mutable_elapsed() = GetCurrentTime() - start_time;
  }

  bool IsFullMatch(const distlock::LockMatchExpression &expression,
                   const distlock::Lock &lock,
                   const Timestamp &current_time) const {
    // TODO(daeyun): Consider precompiling regex.
    auto is_regex_match =
        RE2::FullMatch(lock.global_id(), expression.global_id_regex());
    auto is_expiration_ok =
        expression.is_expired() == IsLockExpired(lock, current_time);
    return is_expiration_ok and is_regex_match;
  }

  void ListLocks(const distlock::ListLocksRequest *request,
                 distlock::ListLocksResponse *response) const {
    const auto start_time = GetCurrentTime();
    leveldb::Iterator *it = get_db()->NewIterator(leveldb::ReadOptions());

    if (request->start_key().empty()) {
      it->SeekToFirst();
    } else {
      it->Seek(request->start_key());
    }

    const auto &limit = request->end_key();
    auto has_next = limit.empty()
                        ? std::function<bool()>([&it] { return it->Valid(); })
                        : std::function<bool()>([&it, &limit] {
                            return it->Valid() && it->key().ToString() < limit;
                          });


    size_t output_size = 0;

    for (; has_next(); it->Next()) {
      distlock::Lock lock;
      Ensures(lock.ParseFromArray(it->value().data(), it->value().size()));
      const auto unary = [&lock, this](
          const distlock::LockMatchExpression &expr) {
        const auto current_time = GetCurrentTime();
        return IsFullMatch(expr, lock, current_time);
      };
      bool is_included = true;
      if (!request->includes().empty()) {
        is_included = std::any_of(request->includes().begin(),
                                  request->includes().end(), unary);
      }
      bool is_excluded = std::any_of(request->excludes().begin(),
                                     request->excludes().end(), unary);
      if (is_included && !is_excluded) {
        output_size += lock.ByteSizeLong();
        *response->mutable_locks()->Add() = lock;
        if (output_size >= 3145728) {
          // Return before it gets too big. Max is 4MB.
          // Contains around 57000 results with an average key size of 30~40
          // bytes.
          break;
        }
      }
    }

    Ensures(it->status().ok());
    delete it;
    *response->mutable_elapsed() = GetCurrentTime() - start_time;
  }

  void CountLocks(const distlock::CountLocksRequest *request,
                 distlock::CountLocksResponse *response) const {
    const auto start_time = GetCurrentTime();
    leveldb::Iterator *it = get_db()->NewIterator(leveldb::ReadOptions());

    if (request->start_key().empty()) {
      it->SeekToFirst();
    } else {
      it->Seek(request->start_key());
    }

    const auto &limit = request->end_key();
    auto has_next = limit.empty()
                        ? std::function<bool()>([&it] { return it->Valid(); })
                        : std::function<bool()>([&it, &limit] {
                            return it->Valid() && it->key().ToString() < limit;
                          });

    size_t num_expired = 0;
    size_t num_unexpired = 0;
    size_t num_no_expiration = 0;

    for (; has_next(); it->Next()) {
      distlock::Lock lock;
      Ensures(lock.ParseFromArray(it->value().data(), it->value().size()));

      using google::protobuf::util::TimeUtil;
      if (not lock.has_expires_in() or
          TimeUtil::DurationToNanoseconds(lock.expires_in()) == 0) {
        ++num_no_expiration;
      } else {
        const auto current_time = GetCurrentTime();
        if (lock.acquired_since() + lock.expires_in() <= current_time) {
          ++num_expired;
        } else {
          ++num_unexpired;
        }
      }
    }

    Ensures(it->status().ok());
    delete it;

    response->set_expired(num_expired);
    response->set_unexpired(num_unexpired);
    response->set_no_expiration(num_no_expiration);
    *response->mutable_elapsed() = GetCurrentTime() - start_time;
  }

  void GetCurrentServerTime(
      const distlock::GetCurrentServerTimeRequest *request,
      distlock::GetCurrentServerTimeResponse *response) const {
    // `request` is empty for now.
    *response->mutable_server_time() = GetCurrentTime();
  }

 private:
  // Managed externally.
  leveldb::DB *db_ = nullptr;
};

int main(int argc, char *argv[]) {
  spdlog::stdout_color_mt("console");
  using cxxopts::value;
  cxxopts::Options opts("distlock", "Distributed lock service.");
  opts.add_options()
    ("db", "Path to LevelDB database", value<std::string>())
    ("cache_size", "LRU cache size in MB", value<int>()->default_value("200"))
    ("port", "HTTP service port", value<int>()->default_value("22113"))
    ("host", "Hostname", value<std::string>()->default_value("127.0.0.1"))
    ("h,help", "Print usage");
  auto arg = opts.parse(argc, argv);

  if (arg.count("help")) {
    std::cout << opts.help() << std::endl;
    exit(EXIT_SUCCESS);
  }
  if (!arg.count("db")) {
    LOGGER->error("--db is a required argument");
    exit(EXIT_FAILURE);
  }

  LOGGER->info("Starting distlock server");

  leveldb::DB *db;
  leveldb::Options options;
  options.create_if_missing = true;
  options.filter_policy = leveldb::NewBloomFilterPolicy(10);
  options.block_cache =
      leveldb::NewLRUCache(arg["cache_size"].as<int>() * 1048576);

  LOGGER->info("Database: {}", arg["db"].as<std::string>());
  LOGGER->info("Cache size: {} MB", arg["cache_size"].as<int>());

  leveldb::Status status =
      leveldb::DB::Open(options, arg["db"].as<std::string>(), &db);
  if (!status.ok()) {
    LOGGER->error("LevelDB status not OK:\n{}", status.ToString());
    exit(EXIT_FAILURE);
  }

  std::string server_address(arg["host"].as<string>() + ":" +
                             std::to_string(arg["port"].as<int>()));
  LockManagerServiceImpl service;
  service.set_db(db);

  ServerBuilder builder;
  // https://grpc.github.io/grpc/cpp/classgrpc_1_1_server_builder.html
  constexpr int kSeconds = 1000;
  builder.AddChannelArgument(GRPC_ARG_KEEPALIVE_TIME_MS, 10 * kSeconds);
  builder.AddChannelArgument(GRPC_ARG_KEEPALIVE_TIMEOUT_MS, 5 * kSeconds);
  builder.AddChannelArgument(GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS, 1);
  builder.AddChannelArgument(GRPC_ARG_HTTP2_BDP_PROBE, 1);
  builder.AddChannelArgument(GRPC_ARG_ALLOW_REUSEPORT, 0);

  builder.SetSyncServerOption(ServerBuilder::SyncServerOption::MAX_POLLERS, 5);
  builder.SetSyncServerOption(ServerBuilder::SyncServerOption::CQ_TIMEOUT_MSEC,
                              20000);

  builder.AddListeningPort(server_address, grpc::InsecureServerCredentials());
  builder.RegisterService(&service);  // Synchronous
  std::unique_ptr<Server> server(builder.BuildAndStart());
  LOGGER->info("Server listening on {}", server_address);

  // Make sure port is not already being used.
  server->Wait();

  delete db;
  LOGGER->info("Closed");

  return 0;
}
